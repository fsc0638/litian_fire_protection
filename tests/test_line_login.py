"""LINE 登入：與 LINE 的協定細節（PKCE、換 token、驗 ID token）、登入流程的每條分支、帳號管理權限。
不連 LINE、不連資料庫：LINE 用 httpx.MockTransport 假造，資料庫函式用記憶體替身（實際 SQL 在 test_db_integration.py）。"""

import time
from contextlib import contextmanager
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from litian import api
from litian import auth as AU
from litian import line_login as LL

CFG = LL.Config("1234567890", "channel-secret-for-test", "https://review.example.test/api/auth/line/callback")
SUB = "U" + "0123456789abcdef" * 2
ENV = ("LINE_LOGIN_CHANNEL_ID", "LINE_LOGIN_CHANNEL_SECRET", "LINE_LOGIN_CALLBACK_URL")


# ── 與 LINE 的協定 ────────────────────────────────────────────────────

def test_config_from_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert LL.Config.from_env() is None                                       # 都沒設定＝不啟用
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "1234567890")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_SECRET", "s")
    with pytest.raises(ValueError, match="LINE_LOGIN_CALLBACK_URL"):          # 只填一部分：明確說缺哪個
        LL.Config.from_env()
    monkeypatch.setenv("LINE_LOGIN_CALLBACK_URL", "http://x.test/cb")
    with pytest.raises(ValueError):                                           # 回呼網址必須 https
        LL.Config.from_env()
    monkeypatch.setenv("LINE_LOGIN_CALLBACK_URL", CFG.callback_url)
    assert LL.Config.from_env().base_url == "https://review.example.test"


def test_pkce_challenge_matches_rfc7636_example():
    assert LL.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorize_url_parameters():
    u = urlsplit(LL.authorize_url(CFG, "st", "no", "v" * 50))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert f"{u.scheme}://{u.netloc}{u.path}" == LL.AUTHORIZE_URL
    assert q == {"response_type": "code", "client_id": "1234567890", "redirect_uri": CFG.callback_url, "state": "st",
                 "scope": "openid profile", "nonce": "no", "code_challenge": LL.code_challenge("v" * 50),
                 "code_challenge_method": "S256"}
    q2 = parse_qs(urlsplit(LL.authorize_url(CFG, "st", "no", "v" * 50, no_auto_login=True)).query)
    assert q2["disable_auto_login"] == ["true"]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_sends_pkce_and_secret():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), form=parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "a", "id_token": "jwt", "token_type": "Bearer", "new_field": 1})
    assert LL.exchange_code(CFG, "code-1", "verifier-1", _client(handler)) == "jwt"
    f = {k: v[0] for k, v in seen["form"].items()}
    assert seen["url"] == LL.TOKEN_URL and f == {"grant_type": "authorization_code", "code": "code-1", "redirect_uri": CFG.callback_url,
                                                  "client_id": "1234567890", "client_secret": "channel-secret-for-test",
                                                  "code_verifier": "verifier-1"}


@pytest.mark.parametrize("response,transient", [
    (httpx.Response(400, json={"error": "invalid_grant"}), False),
    (httpx.Response(200, json={"access_token": "a"}), False),                # 沒有 id_token
    (httpx.Response(200, json=[]), False),                                   # 不是 JSON 物件
    (httpx.Response(200, json=None), False),
    (httpx.Response(503, json=["maintenance"]), True),
    (httpx.Response(429, text="<html>Too Many</html>"), True),
    (httpx.Response(502, text="bad gateway"), True),
])
def test_exchange_code_failures_are_line_errors(response, transient):
    with pytest.raises(LL.LineError) as e:
        LL.exchange_code(CFG, "c", "v", _client(lambda r: response))
    assert e.value.transient is transient


def test_connection_failure_is_transient():
    with pytest.raises(LL.LineError, match="連線失敗") as e:
        LL.exchange_code(CFG, "c", "v", _client(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    assert e.value.transient


def _claims(**kw):
    c = {"iss": LL.ISSUER, "sub": SUB, "aud": "1234567890", "exp": int(time.time()) + 600, "iat": int(time.time()),
         "nonce": "n-1", "amr": ["linesso"], "name": "王小明", "picture": "https://example.test/p.png"}
    c.update(kw)
    return c


def test_verify_id_token_checks_every_claim():
    seen = {}

    def ok(req):
        seen.update(url=str(req.url), form={k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(200, json=_claims())
    assert LL.verify_id_token(CFG, "jwt", "n-1", _client(ok)) == {"sub": SUB, "name": "王小明"}
    assert seen["url"] == LL.VERIFY_URL and seen["form"] == {"id_token": "jwt", "client_id": "1234567890", "nonce": "n-1"}
    for bad, what in ((_claims(iss="https://evil.test"), "iss"), (_claims(aud="999"), "aud"),
                      (_claims(aud=["999", "1234567890"]), "aud"), (_claims(nonce="other"), "nonce"),
                      (_claims(exp=int(time.time()) - 5), "exp"), (_claims(sub="not-a-line-id"), "sub"),
                      (_claims(sub=SUB + "\n"), "sub")):
        with pytest.raises(LL.LineError, match=what):
            LL.verify_id_token(CFG, "jwt", "n-1", _client(lambda r, b=bad: httpx.Response(200, json=b)))
    assert LL.verify_id_token(CFG, "jwt", "n-1", _client(lambda r: httpx.Response(200, json=_claims(aud=["1234567890"]))))
    with pytest.raises(LL.LineError, match="Invalid IdToken"):                # LINE 驗簽失敗
        LL.verify_id_token(CFG, "jwt", "n-1", _client(lambda r: httpx.Response(400, json={
            "error": "invalid_request", "error_description": "Invalid IdToken."})))
    assert LL.verify_id_token(CFG, "jwt", "n-1", _client(lambda r: httpx.Response(200, json=_claims(name=None))))["name"] is None


# ── 登入流程（API）─────────────────────────────────────────────────────

class FakeAuth:
    """資料庫替身：登入暫存狀態（只能用一次、要同一個瀏覽器）、邀請、帳號。"""

    def __init__(self):
        self.states, self.users, self.sessions = {}, {}, {}
        self.invites = {"good-invite-token": {"id": 5, "username": "amy", "role": "reviewer", "kind": "new", "expires_at": "2030-01-01T00:00:00Z"}}
        self.fail_db = False

    def save_login_state(self, c, state, browser, nonce, verifier, invite_id):
        self.states[state] = {"browser": browser, "nonce": nonce, "verifier": verifier, "invite_id": invite_id}

    def take_login_state(self, c, state, browser):
        st = self.states.get(state)
        if not st or st["browser"] != browser:
            return None                                       # 瀏覽器不符：不刪
        del self.states[state]
        return {"nonce": st["nonce"], "verifier": st["verifier"], "invite_id": st["invite_id"]}

    def invite_info(self, c, token):
        return self.invites.get(token)

    def line_login(self, c, sub, name, invite_id=None):
        if self.fail_db:
            raise RuntimeError("db down")
        if invite_id is not None:
            self.users[sub] = {"id": 1, "username": "amy", "role": "reviewer"}
        u = self.users.get(sub)
        if not u:
            raise AU.AuthError("not_registered")
        self.sessions["sess-1"] = u
        return "sess-1", {**u, "line_name": name}

    def session_user(self, c, token):
        return self.sessions.get(token)


@pytest.fixture
def flow(monkeypatch):
    fa = FakeAuth()

    @contextmanager
    def conn():
        yield NS()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    for name in ("save_login_state", "take_login_state", "invite_info", "line_login", "session_user"):
        monkeypatch.setattr(api.AU, name, getattr(fa, name))
    monkeypatch.setattr(api, "_line_cfg", lambda: CFG)
    calls = {}

    def exchange(cfg, code, verifier, client=None):
        calls["exchange"] = (code, verifier)
        if code == "bad-code":
            raise LL.LineError("400")
        if code == "busy-code":
            raise LL.LineError("429", transient=True)
        if code == "weird-code":
            raise KeyError("unexpected")
        return "jwt"

    def verify(cfg, token, nonce, client=None):
        calls["verify"] = nonce
        return {"sub": SUB, "name": "王小明"}
    monkeypatch.setattr(api.LL, "exchange_code", exchange)
    monkeypatch.setattr(api.LL, "verify_id_token", verify)
    api._login_starts.clear()
    c = TestClient(api.app, base_url="https://review.example.test")
    return NS(client=c, fa=fa, calls=calls)


def _query(r):
    return {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


def _start(flow, **kw):
    r = flow.client.get("/api/auth/line/start", follow_redirects=False, **kw)
    assert r.status_code == 303
    return r, _query(r)


def _cookie(r, name):
    return next((x for x in r.headers.get_list("set-cookie") if x.startswith(name + "=")), None)


def test_start_redirects_to_line_with_browser_bound_state(flow):
    r, q = _start(flow)
    assert r.headers["location"].startswith(LL.AUTHORIZE_URL) and r.headers["cache-control"] == "no-store"
    assert "disable_auto_login" not in q                                      # 一般登入保留自動登入
    st = flow.fa.states[q["state"]]
    assert q["nonce"] == st["nonce"] and q["code_challenge"] == LL.code_challenge(st["verifier"]) and st["invite_id"] is None
    assert 43 <= len(st["verifier"]) <= 128
    sc = _cookie(r, AU.LOGIN_COOKIE).lower()
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc and "path=/" in sc and "domain" not in sc  # __Host- 規則
    assert AU.LOGIN_COOKIE.startswith("__Host-") and AU.COOKIE.startswith("__Host-")
    # 同一瀏覽器再按一次（另一個分頁）：沿用同一個瀏覽器識別，兩個登入都能完成
    _, q2 = _start(flow)
    assert flow.fa.states[q2["state"]]["browser"] == st["browser"]


def test_start_from_other_host_goes_to_canonical_address(flow):
    r = flow.client.get("/api/auth/line/start", headers={"host": "old.example.test"}, follow_redirects=False)
    assert r.headers["location"] == "https://review.example.test/workbench" and not flow.fa.states


def test_callback_success_sets_session_cookie(flow):
    _, q = _start(flow)
    flow.fa.users[SUB] = {"id": 1, "username": "amy", "role": "reviewer"}
    r = flow.client.get("/api/auth/line/callback", params={"code": "c-1", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/workbench"
    sess = _cookie(r, AU.COOKIE).lower()
    assert "=sess-1" in sess and "httponly" in sess and "secure" in sess and "samesite=lax" in sess and "path=/" in sess
    assert "max-age=0" in _cookie(r, AU.LOGIN_COOKIE).lower()                # 登入暫存清掉
    assert flow.calls["exchange"][0] == "c-1" and flow.calls["verify"] == q["nonce"]
    assert flow.client.get("/api/auth/me").json()["username"] == "amy"
    r2 = flow.client.get("/api/auth/line/callback", params={"code": "c-1", "state": q["state"]}, follow_redirects=False)
    assert r2.headers["location"] == "/workbench?login_error=expired"        # 同一個 state 不能再用一次


def test_callback_from_other_browser_is_rejected_without_consuming_state(flow):
    _, q = _start(flow)
    mine = flow.client.cookies.get(AU.LOGIN_COOKIE)
    flow.client.cookies.set(AU.LOGIN_COOKIE, "someone-elses-browser-xxxxxxxx")
    r = flow.client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/workbench?login_error=expired" and "exchange" not in flow.calls   # 不會拿 code 去換 token
    assert q["state"] in flow.fa.states                                       # 本人的登入不受影響
    flow.client.cookies.set(AU.LOGIN_COOKIE, mine)
    flow.fa.users[SUB] = {"id": 1, "username": "amy", "role": "reviewer"}
    r = flow.client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/workbench"


@pytest.mark.parametrize("params,code", [
    ({"error": "ACCESS_DENIED", "error_description": "The resource owner denied the request."}, "denied"),
    ({"error": "access_denied"}, "denied"),
    ({"error": "SERVER_ERROR"}, "line_error"),
    ({"error": "x\nforged log line"}, "line_error"),                         # 不合格式的 error 不原樣寫進日誌
    ({"code": "bad-code"}, "line_error"),
    ({"code": "busy-code"}, "line_unavailable"),                             # LINE 暫時無法服務
    ({"code": "weird-code"}, "line_error"),                                  # 意外例外也回工作台，不給 500
    ({"code": "c"}, "not_registered"),                                       # LINE 驗證通過，但沒有開通
])
def test_callback_failures_map_to_error_codes(flow, params, code):
    _, q = _start(flow)
    r = flow.client.get("/api/auth/line/callback", params={**params, "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/workbench?login_error={code}"
    assert _cookie(r, AU.COOKIE) is None


def test_callback_database_error_is_not_500(flow):
    _, q = _start(flow)
    flow.fa.fail_db = True
    r = flow.client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/workbench?login_error=line_error"


def test_forged_callbacks_do_not_lock_anyone_out(flow):
    """偽造的回呼（例：別的網頁放 50 張圖片打回呼網址）不會讓同一個辦公室 IP 的人登不進來。"""
    for _ in range(50):
        flow.client.get("/api/auth/line/callback", params={"code": "c", "state": "forged"}, follow_redirects=False,
                        headers={"X-Forwarded-For": "203.0.113.7"})
    r = flow.client.get("/api/auth/line/start", follow_redirects=False, headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.headers["location"].startswith(LL.AUTHORIZE_URL)
    assert flow.client.post("/api/auth/invite", json={"token": "good-invite-token"}).status_code == 200


def test_start_refuses_when_too_many_logins_pending(flow, monkeypatch):
    def full(*a):
        raise AU.AuthError("busy")
    monkeypatch.setattr(api.AU, "save_login_state", full)
    assert _start(flow)[0].headers["location"] == "/workbench?login_error=busy"


def test_invite_start_auto_login_first_then_no_auto_retry(flow):
    r = flow.client.post("/api/auth/line/start", data={"invite": "nope-nope-nope"}, follow_redirects=False)
    assert r.headers["location"] == "/workbench?login_error=invite_invalid"
    r = flow.client.post("/api/auth/line/start", data={"invite": "good-invite-token"}, follow_redirects=False)
    q = _query(r)
    assert "disable_auto_login" not in q                                      # 第一次：手機上一鍵自動登入
    assert flow.fa.states[q["state"]]["invite_id"] == 5 and "good-invite-token" not in r.headers["location"]
    r2 = flow.client.post("/api/auth/line/start", data={"invite": "good-invite-token", "noauto": "1"}, follow_redirects=False)
    assert _query(r2)["disable_auto_login"] == "true"                        # 失敗後重試：不自動登入
    r = flow.client.get("/api/auth/line/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/workbench" and flow.fa.users[SUB]["username"] == "amy"


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document"},
                                     {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "document"},
                                     {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "image"}])
def test_start_only_from_real_page_navigation(flow, headers):
    """別的網站用圖片、iframe 或連結觸發「發起登入」：不建立暫存（灌不爆全站上限），也不算任何人的次數。"""
    r = flow.client.get("/api/auth/line/start", headers=headers, follow_redirects=False)
    assert r.headers["location"] == "/workbench?login_error=expired" and not flow.fa.states
    ok = flow.client.get("/api/auth/line/start", headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "document"},
                         follow_redirects=False)
    assert ok.headers["location"].startswith(LL.AUTHORIZE_URL)


def test_start_rate_limit_per_source(flow, monkeypatch):
    monkeypatch.setattr(api, "LOGIN_START_MAX", 3)
    api._login_starts.clear()
    h = {"X-Forwarded-For": "198.51.100.9"}
    locs = [flow.client.get("/api/auth/line/start", headers=h, follow_redirects=False).headers["location"] for _ in range(4)]
    assert all(x.startswith(LL.AUTHORIZE_URL) for x in locs[:3]) and locs[3] == "/workbench?login_error=slow_down"
    assert flow.client.get("/api/auth/line/start", headers={"X-Forwarded-For": "198.51.100.10"},
                           follow_redirects=False).headers["location"].startswith(LL.AUTHORIZE_URL)   # 別的來源不受影響
    api._login_starts.clear()


def test_invite_lookup(flow):
    r = flow.client.post("/api/auth/invite", json={"token": "nope-nope-nope"})
    assert r.status_code == 404 and "直接用 LINE 登入" in r.json()["detail"]
    r = flow.client.post("/api/auth/invite", json={"token": "good-invite-token"})
    assert r.status_code == 200 and r.json()["username"] == "amy" and r.json()["kind"] == "new" and r.json()["line_login"]


def test_not_configured(flow, monkeypatch):
    monkeypatch.setattr(api, "_line_cfg", lambda: None)
    assert _start(flow)[0].headers["location"] == "/workbench?login_error=not_configured"
    assert flow.client.get("/api/auth/options").json() == {"line_login": False}


def test_workbench_page_headers_and_no_password_form(flow):
    r = flow.client.get("/workbench")
    assert r.headers["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert 'type="password"' not in r.text and 'action="/api/auth/line/start"' in r.text
    assert "#invite=" in r.text and "?invite=" not in r.text                  # 邀請權杖在 # 片段
    assert 'name="noauto" value="1"' in r.text                               # 開通失敗後可改用不自動登入


# ── 帳號管理權限 ─────────────────────────────────────────────────────

@pytest.fixture
def admin_api(monkeypatch):
    @contextmanager
    def conn():
        yield NS()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    users = {"admin-token": {"id": 1, "username": "boss", "role": "admin", "line_name": None},
             "rev-token": {"id": 2, "username": "amy", "role": "reviewer", "line_name": None}}
    monkeypatch.setattr(api.AU, "session_user", lambda c, t: users.get(t))
    monkeypatch.setattr(api.AU, "list_users", lambda c: [])
    monkeypatch.setattr(api.AU, "pending_invites", lambda c: [])
    made = []

    def create_invite(c, username, role, by, hours, mode=None, created_by_id=None):
        if username == "停用者":
            raise ValueError("這個帳號已停用，請先啟用再發邀請")
        if username == "amy" and mode == "new":
            raise AU.Conflict("帳號「amy」已存在")
        made.append((username, role, by, hours, mode, created_by_id))
        return "tok-123", {"id": 9, "username": username, "role": role, "kind": "new" if mode == "new" else "bind",
                           "expires_at": "2030-01-01T00:00:00Z"}
    monkeypatch.setattr(api.AU, "create_invite", create_invite)
    monkeypatch.setattr(api.AU, "revoke_invite", lambda c, i: i == 9)
    monkeypatch.setattr(api.AU, "update_user", lambda c, uid, disabled=None, role=None:
                        {"id": uid, "username": "x", "role": role or "reviewer", "disabled": bool(disabled)})
    monkeypatch.setattr(api, "_line_cfg", lambda: CFG)
    return NS(client=TestClient(api.app, base_url="https://review.example.test"), made=made)


def test_admin_endpoints_require_admin(admin_api):
    c = admin_api.client
    for method, path, body in (("get", "/api/admin/users", None), ("post", "/api/admin/invites", {"username": "bob"}),
                               ("delete", "/api/admin/invites/9", None), ("patch", "/api/admin/users/3", {"disabled": True})):
        assert c.request(method.upper(), path, json=body).status_code == 401           # 未登入
        c.cookies.set(AU.COOKIE, "rev-token")
        assert c.request(method.upper(), path, json=body).status_code == 403           # 審圖人員不行
        c.cookies.clear()
    assert admin_api.made == []


def test_admin_invite_modes_revoke_and_self_protection(admin_api, monkeypatch):
    c = admin_api.client
    c.cookies.set(AU.COOKIE, "admin-token")
    r = c.post("/api/admin/invites", json={"username": "bob", "role": "admin", "hours": 72})
    assert r.status_code == 200 and r.json()["url"] == "https://review.example.test/workbench#invite=tok-123"
    assert admin_api.made == [("bob", "admin", "boss", 72, "new", 1)]                    # 記下是誰發的
    r = c.post("/api/admin/invites", json={"username": "amy", "role": "admin"})         # 開新帳號但名稱已存在：不會悄悄變換綁
    assert r.status_code == 409 and "已存在" in r.json()["detail"]
    assert c.post("/api/admin/invites", json={"username": "amy", "mode": "rebind"}).status_code == 200
    assert c.post("/api/admin/invites", json={"username": "boss", "mode": "rebind"}).status_code == 422   # 不能替自己換綁
    assert c.post("/api/admin/invites", json={"username": "停用者"}).status_code == 422
    assert c.post("/api/admin/invites", json={"username": "bob", "hours": AU.INVITE_MAX_HOURS}).status_code == 200      # 上限 90 天剛好可以
    assert c.post("/api/admin/invites", json={"username": "bob", "hours": AU.INVITE_MAX_HOURS + 1}).status_code == 422  # 多 1 小時就拒絕
    assert c.post("/api/admin/invites", json={"username": "bob", "hours": 0}).status_code == 422
    assert c.post("/api/admin/invites", json={"username": "bob", "role": "root"}).status_code == 422
    assert c.delete("/api/admin/invites/9").status_code == 200 and c.delete("/api/admin/invites/8").status_code == 404
    assert c.patch("/api/admin/users/1", json={"disabled": True}).status_code == 422           # 不能停用自己
    assert c.patch("/api/admin/users/1", json={"role": "reviewer"}).status_code == 422         # 不能取消自己的管理者
    assert c.patch("/api/admin/users/2", json={"disabled": True}).json()["disabled"] is True
    monkeypatch.setattr(api, "_line_cfg", lambda: None)
    assert c.post("/api/admin/invites", json={"username": "bob"}).status_code == 409           # LINE 未設定時不發連結
