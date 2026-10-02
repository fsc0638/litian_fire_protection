"""工作台帳號與登入：LINE 登入＋管理者發的一次性邀請連結（不使用密碼）。

- 開通：管理者在工作台「帳號管理」替新帳號產生邀請連結（預設 24 小時內有效、只能用一次）；
  同仁點連結用 LINE 登入，該 LINE 帳號就綁定到這個帳號。換手機、換 LINE 帳號時，由帳號列表的
  「重新綁定 LINE」另發換綁連結（角色不變，原本的登入全部登出）。
- 之後同仁直接「用 LINE 登入」：以 LINE 使用者 ID（同一 provider 內固定不變）找帳號；沒綁定的 LINE 帳號一律進不來。
- 邀請連結的權杖放在網址的 # 片段（不會送到伺服器、不進存取紀錄）；邀請權杖、登入權杖在資料庫都只存 SHA-256；
  LINE 的 access token 用完即丟，不保存。
- 管理者被停用或降級時，他發出、還沒用掉的邀請一併作廢；兌換時也再確認發出者仍是啟用中的管理者。
- 主機命令列（第一位管理者、或網頁進不去時用；在 /opt/litian 底下執行）：
    sudo docker compose exec api python -m litian.auth invite 帳號 [--role admin] [--hours 24]
    sudo docker compose exec api python -m litian.auth list
    sudo docker compose exec api python -m litian.auth disable|enable 帳號
    sudo docker compose exec api python -m litian.auth role 帳號 admin|reviewer
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

# 已存在的資料表只在需要時才 ALTER（ALTER 會拿 app_user 的排他鎖；每次啟動都拿，遇到長交易會卡住整個工作台）。
# 第一次從密碼版遷移時：移除密碼欄位，並登出所有密碼時代的登入（使用者決定完全移除密碼登入）。
SCHEMA = """
SET LOCAL lock_timeout = '10s';
CREATE TABLE IF NOT EXISTS app_user (
  id bigserial PRIMARY KEY,
  username text NOT NULL UNIQUE,
  role text NOT NULL DEFAULT 'reviewer',
  disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app_session (
  token_hash text PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'app_user' AND column_name = 'line_user_id') THEN
    ALTER TABLE app_user ADD COLUMN line_user_id text UNIQUE, ADD COLUMN line_name text, ADD COLUMN last_login_at timestamptz;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'app_user' AND column_name = 'password_hash') THEN
    DELETE FROM app_session;
    ALTER TABLE app_user DROP COLUMN password_hash;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS app_invite (
  id bigserial PRIMARY KEY,
  token_hash text NOT NULL UNIQUE,
  username text NOT NULL,
  role text NOT NULL,
  kind text NOT NULL,
  created_by text NOT NULL,
  created_by_id bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  revoked_at timestamptz
);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'app_invite' AND column_name = 'created_by_id') THEN
    ALTER TABLE app_invite ADD COLUMN created_by_id bigint;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS login_state (
  state_hash text PRIMARY KEY,
  browser_hash text NOT NULL,
  nonce text NOT NULL,
  code_verifier text NOT NULL,
  invite_id bigint REFERENCES app_invite ON DELETE CASCADE,
  expires_at timestamptz NOT NULL
);
"""
ROLES = ("reviewer", "admin")
ROLE_LABEL = {"reviewer": "審圖人員", "admin": "管理者"}
SESSION_HOURS = 12
COOKIE = "__Host-fr_session"          # __Host-：只能由本站設定（同網域的其他子網域塞不進來），必須 Secure、Path=/
LOGIN_COOKIE = "__Host-fr_login"      # 綁定「哪個瀏覽器發起登入」，防止登入 CSRF
STATE_MINUTES = 10                    # LINE 授權碼也只有 10 分鐘
MAX_PENDING_LOGINS = 5000             # 進行中的登入暫存上限（全站；防止灌爆資料表，不用來源 IP 封鎖）
INVITE_HOURS, INVITE_MAX_HOURS = 24, 168
USERNAME_RE = re.compile(r"[A-Za-z0-9_.\-一-鿿]{2,32}")
ADMIN_LOCK = 7_310_001                # 改角色／停用時的全域鎖鍵（避免兩位管理者同時互相停用）


class AuthError(Exception):
    """登入失敗；code 給前端對應訊息（不帶細節）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Conflict(ValueError):
    """與現況衝突（例：開新帳號但名稱已存在），API 回 409。"""


def ensure_schema(conn) -> None:
    with conn.transaction():
        conn.execute(SCHEMA)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("帳號名稱只能用中文、英數字與 _ . -，2 到 32 字")
    return username


# ── 邀請 ─────────────────────────────────────────────────────────────

def create_invite(conn, username: str, role: str, created_by: str, hours: int = INVITE_HOURS, *,
                  mode: str | None = None, created_by_id: int | None = None) -> tuple[str, dict]:
    """產生一次性邀請，回傳（權杖, 邀請資料）。權杖只在此時出現一次，資料庫只存雜湊。
    mode="new"：開新帳號，名稱已存在就拒絕；mode="rebind"：既有帳號重新綁定 LINE（角色不變）；
    mode=None（主機命令列）：依帳號是否存在自動判斷。同一帳號先前未使用的邀請一律作廢。"""
    username = check_username(username)
    if role not in ROLES:
        raise ValueError(f"角色只能是 {'／'.join(ROLES)}")
    if not 1 <= int(hours) <= INVITE_MAX_HOURS:
        raise ValueError(f"有效時間要在 1 到 {INVITE_MAX_HOURS} 小時之間")
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('invite:' || %s))", (username,))   # 同名同時發：排隊
        u = conn.execute("SELECT role, disabled FROM app_user WHERE username = %s", (username,)).fetchone()
        if mode == "new" and u:
            raise Conflict(f"帳號「{username}」已存在。要讓他換手機或換 LINE 帳號，請在帳號列表按「重新綁定 LINE」")
        if mode == "rebind" and not u:
            raise Conflict(f"沒有帳號「{username}」")
        if u and u["disabled"]:
            raise ValueError("這個帳號已停用，請先啟用再發邀請")
        kind = "bind" if u else "new"
        conn.execute("UPDATE app_invite SET revoked_at = now() WHERE username = %s AND used_at IS NULL AND revoked_at IS NULL",
                     (username,))
        row = conn.execute(
            "INSERT INTO app_invite (token_hash, username, role, kind, created_by, created_by_id, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, username, role, kind, expires_at",
            (_hash(token), username, u["role"] if u else role, kind, created_by, created_by_id,
             _now() + timedelta(hours=int(hours)))).fetchone()
    return token, dict(row)


def invite_info(conn, token: str | None) -> dict | None:
    """有效（未使用、未作廢、未過期）的邀請；否則 None。"""
    if not token or len(token) > 200:
        return None
    return conn.execute(
        "SELECT id, username, role, kind, expires_at FROM app_invite WHERE token_hash = %s "
        "AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now()", (_hash(token),)).fetchone()


def revoke_invite(conn, invite_id: int) -> bool:
    return conn.execute("UPDATE app_invite SET revoked_at = now() WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL",
                        (invite_id,)).rowcount == 1


def pending_invites(conn) -> list[dict]:
    return conn.execute("SELECT id, username, role, kind, created_by, created_at, expires_at FROM app_invite "
                        "WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now() ORDER BY id DESC").fetchall()


# ── LINE 登入的暫存狀態（state、nonce、PKCE）─────────────────────────────

def save_login_state(conn, state: str, browser: str, nonce: str, verifier: str, invite_id: int | None) -> None:
    conn.execute("DELETE FROM login_state WHERE expires_at < now()")
    if conn.execute("SELECT count(*) AS n FROM login_state").fetchone()["n"] >= MAX_PENDING_LOGINS:
        raise AuthError("busy")
    conn.execute("INSERT INTO login_state (state_hash, browser_hash, nonce, code_verifier, invite_id, expires_at) "
                 "VALUES (%s, %s, %s, %s, %s, %s)",
                 (_hash(state), _hash(browser), nonce, verifier, invite_id, _now() + timedelta(minutes=STATE_MINUTES)))


def take_login_state(conn, state: str | None, browser: str | None) -> dict | None:
    """取出並刪除（只能用一次）。要同一個瀏覽器（Cookie 相符）且未過期才算數；
    Cookie 不符時不刪（同一瀏覽器同時開兩個登入時，另一個仍可完成）。"""
    if not state or not browser or len(state) > 200 or len(browser) > 200:
        return None
    row = conn.execute("DELETE FROM login_state WHERE state_hash = %s AND browser_hash = %s "
                       "RETURNING nonce, code_verifier, invite_id, expires_at", (_hash(state), _hash(browser))).fetchone()
    if not row or row["expires_at"] <= _now():
        return None
    return {"nonce": row["nonce"], "verifier": row["code_verifier"], "invite_id": row["invite_id"]}


# ── 登入與登入狀態 ───────────────────────────────────────────────────

def _redeem(conn, invite_id: int, sub: str, name: str | None) -> None:
    """用掉邀請並綁定 LINE；任何一步不符就整筆還原（邀請仍可再用）。"""
    import psycopg
    try:
        with conn.transaction():
            inv = conn.execute("UPDATE app_invite SET used_at = now() WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL "
                               "AND expires_at > now() RETURNING username, role, kind, created_by_id", (invite_id,)).fetchone()
            if not inv:
                raise AuthError("invite_invalid")
            if inv["created_by_id"] is not None and not conn.execute(
                    "SELECT 1 FROM app_user WHERE id = %s AND role = 'admin' AND NOT disabled", (inv["created_by_id"],)).fetchone():
                raise AuthError("invite_invalid")              # 發出者已不是啟用中的管理者
            other = conn.execute("SELECT username FROM app_user WHERE line_user_id = %s", (sub,)).fetchone()
            if other and other["username"] != inv["username"]:
                raise AuthError("line_taken")                   # 這個 LINE 帳號已綁定別的帳號
            if inv["kind"] == "new":
                row = conn.execute("INSERT INTO app_user (username, role, line_user_id, line_name) VALUES (%s, %s, %s, %s) "
                                   "ON CONFLICT (username) DO NOTHING RETURNING id",
                                   (inv["username"], inv["role"], sub, name)).fetchone()
            else:
                row = conn.execute("UPDATE app_user SET line_user_id = %s, line_name = %s WHERE username = %s AND NOT disabled "
                                   "RETURNING id", (sub, name, inv["username"])).fetchone()
                if row:
                    conn.execute("DELETE FROM app_session WHERE user_id = %s", (row["id"],))   # 換綁：舊的登入全部登出
            if not row:
                raise AuthError("invite_invalid")               # 帳號名稱已被用走，或帳號已停用
    except psycopg.errors.UniqueViolation:
        raise AuthError("line_taken")                           # 同一個 LINE 帳號同時兌換兩張邀請


def line_login(conn, sub: str, name: str | None, invite_id: int | None = None) -> tuple[str, dict]:
    """LINE 驗證通過後：有邀請就先開通／換綁，再以 LINE 使用者 ID 找帳號、建立登入狀態。"""
    if invite_id is not None:
        _redeem(conn, invite_id, sub, name)
    u = conn.execute("SELECT id, username, role, disabled FROM app_user WHERE line_user_id = %s", (sub,)).fetchone()
    if not u:
        raise AuthError("not_registered")
    if u["disabled"]:
        raise AuthError("disabled")
    token = secrets.token_urlsafe(32)
    # 建立登入時再確認一次綁定仍是這個 LINE 帳號（換綁剛好同時完成時，舊 LINE 不能拿到登入）
    ok = conn.execute("INSERT INTO app_session (token_hash, user_id, expires_at) SELECT %s, id, %s FROM app_user "
                      "WHERE id = %s AND line_user_id = %s AND NOT disabled RETURNING user_id",
                      (_hash(token), _now() + timedelta(hours=SESSION_HOURS), u["id"], sub)).fetchone()
    if not ok:
        raise AuthError("not_registered")
    conn.execute("UPDATE app_user SET line_name = %s, last_login_at = now() WHERE id = %s", (name, u["id"]))
    conn.execute("DELETE FROM app_session WHERE expires_at < now()")
    return token, {"id": u["id"], "username": u["username"], "role": u["role"], "line_name": name}


def session_user(conn, token: str | None) -> dict | None:
    if not token or len(token) > 200:
        return None
    return conn.execute(
        "SELECT u.id, u.username, u.role, u.line_name FROM app_session s JOIN app_user u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled", (_hash(token),)).fetchone()


def logout(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM app_session WHERE token_hash = %s", (_hash(token),))


# ── 帳號管理 ─────────────────────────────────────────────────────────

def list_users(conn) -> list[dict]:
    return conn.execute("SELECT id, username, role, disabled, line_user_id IS NOT NULL AS line_bound, line_name, "
                        "created_at, last_login_at FROM app_user ORDER BY id").fetchall()


def update_user(conn, user_id: int, *, disabled: bool | None = None, role: str | None = None) -> dict:
    """停用／啟用、改角色。至少要留一位啟用中的管理者；停用或改角色會讓該帳號立即登出；
    停用或取消管理者時，作廢他發出的、以及要開通他這個帳號的未使用邀請。"""
    if role is not None and role not in ROLES:
        raise ValueError(f"角色只能是 {'／'.join(ROLES)}")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_LOCK,))     # 所有帳號異動排隊，人數檢查才可靠
        u = conn.execute("SELECT id, username, role, disabled FROM app_user WHERE id = %s", (user_id,)).fetchone()
        if not u:
            raise LookupError("沒有這個帳號")
        new_dis = u["disabled"] if disabled is None else bool(disabled)
        new_role = u["role"] if role is None else role
        losing_admin = u["role"] == "admin" and not u["disabled"] and (new_dis or new_role != "admin")
        if losing_admin:
            n = conn.execute("SELECT count(*) AS n FROM app_user WHERE role = 'admin' AND NOT disabled AND id <> %s",
                             (user_id,)).fetchone()["n"]
            if n == 0:
                raise ValueError("至少要保留一位啟用中的管理者")
        conn.execute("UPDATE app_user SET disabled = %s, role = %s WHERE id = %s", (new_dis, new_role, user_id))
        if new_dis != u["disabled"] or new_role != u["role"]:
            conn.execute("DELETE FROM app_session WHERE user_id = %s", (user_id,))
        if losing_admin or (new_dis and not u["disabled"]):
            conn.execute("UPDATE app_invite SET revoked_at = now() WHERE used_at IS NULL AND revoked_at IS NULL "
                         "AND (created_by_id = %s OR username = %s)", (user_id, u["username"]))
    return {"id": u["id"], "username": u["username"], "role": new_role, "disabled": new_dis}


def user_id_of(conn, username: str) -> int | None:
    r = conn.execute("SELECT id FROM app_user WHERE username = %s", (username,)).fetchone()
    return r["id"] if r else None


def invite_link(base_url: str, token: str) -> str:
    """邀請權杖放在 # 片段：不會送到伺服器，不進存取紀錄、Referer 與網址預覽。"""
    return f"{base_url.rstrip('/')}/workbench#invite={token}"


def main(argv: list[str]) -> int:
    import psycopg
    from psycopg.rows import dict_row

    from litian.line_login import Config
    ap = argparse.ArgumentParser(prog="litian.auth", description="工作台帳號管理（主機上執行）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("invite", help="產生一次性邀請連結（新帳號，或既有帳號重新綁定 LINE）")
    i.add_argument("username")
    i.add_argument("--role", default="reviewer", choices=ROLES)
    i.add_argument("--hours", type=int, default=INVITE_HOURS)
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    r = sub.add_parser("role")
    r.add_argument("username")
    r.add_argument("role", choices=ROLES)
    sub.add_parser("list")
    a = ap.parse_args(argv)
    try:
        cfg = Config.from_env()                     # 設定有錯先停，不要先寫入邀請
    except ValueError as e:
        raise SystemExit(str(e))
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        try:
            if a.cmd == "invite":
                ensure_schema(conn)
                token, inv = create_invite(conn, a.username, a.role, "主機命令列", a.hours)
                what = "開新帳號" if inv["kind"] == "new" else "重新綁定 LINE（角色不變，原本的登入會被登出）"
                print(f"{inv['username']}：{what}，角色 {ROLE_LABEL[inv['role']]}，"
                      f"{inv['expires_at'].astimezone(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M}（台北時間）前有效、只能用一次")
                if cfg:
                    print(invite_link(cfg.base_url, token))
                else:
                    print("（LINE 登入尚未設定：設好之前這個連結無法使用。下面是連結的後半段，前面請接上網站網址）")
                    print(invite_link("", token))
            elif a.cmd in ("disable", "enable", "role"):
                uid = user_id_of(conn, a.username)
                if uid is None:
                    raise SystemExit("沒有這個帳號")
                if a.cmd == "role":
                    update_user(conn, uid, role=a.role)
                else:
                    update_user(conn, uid, disabled=a.cmd == "disable")
                print("已更新；該帳號原有的登入已登出")
            else:
                for u in list_users(conn):
                    line = f"LINE：{u['line_name'] or '（已綁定）'}" if u["line_bound"] else "LINE 未綁定"
                    last = f"{u['last_login_at']:%Y-%m-%d}" if u["last_login_at"] else "-"
                    print(f"{u['username']:20s} {u['role']:9s} {'停用' if u['disabled'] else '啟用'}  {line}  最後登入 {last}")
                for v in pending_invites(conn):
                    print(f"（待開通）{v['username']} {'開新帳號' if v['kind'] == 'new' else '重新綁定'} {v['role']} "
                          f"到期 {v['expires_at'].astimezone(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M}")
        except (ValueError, LookupError) as e:
            raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
