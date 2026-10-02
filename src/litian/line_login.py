"""LINE Login v2.1（OAuth 2.0 授權碼＋OpenID Connect）：審核工作台用 LINE 帳號登入。

流程：/api/auth/line/start 產生 state、nonce、PKCE code_verifier → 轉到 LINE 授權頁 →
LINE 帶 code 回 /api/auth/line/callback → 用 code（＋code_verifier、channel secret）換 token →
把 ID token 交給 LINE 官方驗證端點驗簽、核對 nonce → 取得使用者 ID（sub，同一 provider 內固定不變）。

ID token 一律交給 LINE 的 /oauth2/v2.1/verify 驗證，不自行解 JWT：官方文件對網頁登入的簽章演算法說法不一
（HS256／ES256），驗證端點兩種都處理。驗證結果再自行核對 iss、aud、nonce、到期時間與 sub 格式。

設定（環境變數，三個都有才啟用）：LINE_LOGIN_CHANNEL_ID、LINE_LOGIN_CHANNEL_SECRET、
LINE_LOGIN_CALLBACK_URL（完整 https 網址，必須與 LINE Developers 頻道登記的 Callback URL 一致；
不從請求推導，因為反向代理後面看到的常是 http）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

AUTHORIZE_URL = "https://access.line.me/oauth2/v2.1/authorize"
TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
ISSUER = "https://access.line.me"
SCOPE = "openid profile"
SUB_RE = re.compile(r"U[0-9a-f]{32}")
TIMEOUT = 10.0


class LineError(Exception):
    """與 LINE 交換或驗證失敗（訊息只給日誌，不顯示給使用者）。
    transient：LINE 暫時無法服務（429、5xx、連線失敗、逾時），稍後重試即可。"""

    def __init__(self, msg: str, transient: bool = False):
        super().__init__(msg)
        self.transient = transient


@dataclass(frozen=True)
class Config:
    channel_id: str
    channel_secret: str
    callback_url: str

    @property
    def base_url(self) -> str:
        """對外網址（邀請連結用），由 callback 網址取 scheme＋host。"""
        u = urlsplit(self.callback_url)
        return f"{u.scheme}://{u.netloc}"

    @classmethod
    def from_env(cls) -> "Config | None":
        names = ("LINE_LOGIN_CHANNEL_ID", "LINE_LOGIN_CHANNEL_SECRET", "LINE_LOGIN_CALLBACK_URL")
        cid, secret, cb = (os.environ.get(n, "").strip() for n in names)
        missing = [n for n, v in zip(names, (cid, secret, cb)) if not v]
        if len(missing) == 3:
            return None
        if missing:
            raise ValueError("LINE 登入設定不完整，缺少：" + "、".join(missing))
        if not cid.isdigit() or not cb.startswith("https://"):
            raise ValueError("LINE 登入設定格式不符：Channel ID 應為數字、Callback URL 應為 https 網址")
        return cls(cid, secret, cb)


def code_challenge(verifier: str) -> str:
    """PKCE S256：SHA-256 後 Base64URL、去掉 = 補位。"""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def authorize_url(cfg: Config, state: str, nonce: str, verifier: str, *, no_auto_login: bool = False) -> str:
    params = {"response_type": "code", "client_id": cfg.channel_id, "redirect_uri": cfg.callback_url,
              "state": state, "scope": SCOPE, "nonce": nonce,
              "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"}
    if no_auto_login:
        params["disable_auto_login"] = "true"          # 自動登入失敗（例：無痕視窗）後重試時用
    return AUTHORIZE_URL + "?" + urlencode(params)


def _post(client: httpx.Client | None, url: str, data: dict) -> dict:
    try:
        if client is None:
            with httpx.Client(timeout=TIMEOUT) as c:
                r = c.post(url, data=data)
        else:
            r = client.post(url, data=data)
    except httpx.HTTPError as e:
        raise LineError(f"{url} 連線失敗：{type(e).__name__}", transient=True) from e
    rid = r.headers.get("x-line-request-id", "-")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):                     # LINE 或中間代理回 []、null、字串：當作沒有內容
        body = {}
    if r.status_code != 200:
        raise LineError(f"{url} 回應 {r.status_code}（request-id {rid}）：{body.get('error')} {body.get('error_description')}",
                        transient=r.status_code == 429 or r.status_code >= 500)
    return body


def exchange_code(cfg: Config, code: str, verifier: str, client: httpx.Client | None = None) -> str:
    """授權碼 → ID token（授權碼 10 分鐘內有效、只能用一次）。access token 用不到，不保存。"""
    body = _post(client, TOKEN_URL, {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.callback_url,
                                     "client_id": cfg.channel_id, "client_secret": cfg.channel_secret,
                                     "code_verifier": verifier})
    token = body.get("id_token")
    if not isinstance(token, str) or not token:
        raise LineError("token 回應沒有 id_token（scope 未含 openid？）")
    return token


def verify_id_token(cfg: Config, id_token: str, nonce: str, client: httpx.Client | None = None) -> dict:
    """交給 LINE 驗證端點驗簽，再自行核對內容。回傳 {sub, name}（name＝LINE 顯示名稱，可能為 None）。"""
    claims = _post(client, VERIFY_URL, {"id_token": id_token, "client_id": cfg.channel_id, "nonce": nonce})
    aud = claims.get("aud")
    checks = [
        (claims.get("iss") == ISSUER, "iss"),
        (aud == cfg.channel_id or aud == [cfg.channel_id], "aud"),
        (claims.get("nonce") == nonce, "nonce"),
        (isinstance(claims.get("exp"), (int, float)) and claims["exp"] > time.time(), "exp"),
        (isinstance(claims.get("sub"), str) and SUB_RE.fullmatch(claims["sub"]), "sub"),
    ]
    bad = [name for ok, name in checks if not ok]
    if bad:
        raise LineError(f"ID token 內容不符：{'、'.join(bad)}")
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    return {"sub": claims["sub"], "name": (name or "")[:100] or None}
