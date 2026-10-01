"""工作台帳號與登入。

- 密碼：標準函式庫 scrypt 雜湊（每人隨機鹽），資料庫只存雜湊。
- 登入狀態：隨機權杖放在 HttpOnly、Secure、SameSite=Lax 的 Cookie；資料庫只存權杖的 SHA-256。
- 帳號只能由主機上的管理者用命令列建立，密碼由本人輸入（不經過 AI、不進紀錄）：
    sudo docker compose exec api python -m litian.auth create-user 帳號 [--role admin]
    sudo docker compose exec api python -m litian.auth set-password 帳號
    sudo docker compose exec api python -m litian.auth disable 帳號
    sudo docker compose exec api python -m litian.auth list
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
  id bigserial PRIMARY KEY,
  username text NOT NULL UNIQUE,
  password_hash text NOT NULL,
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
"""
ROLES = ("reviewer", "admin")
SESSION_HOURS = 12
COOKIE = "fr_session"
PASSWORD_MIN = 10
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
_N, _R, _P = 2 ** 14, 8, 1


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_new_password(password: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"密碼至少要 {PASSWORD_MIN} 個字元")


def create_user(conn, username: str, password: str, role: str = "reviewer") -> int:
    if not USERNAME_RE.match(username):
        raise ValueError("帳號只能用英數字與 _ . -，2 到 32 字")
    if role not in ROLES:
        raise ValueError(f"角色只能是 {'／'.join(ROLES)}")
    check_new_password(password)
    return conn.execute("INSERT INTO app_user (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                        (username, hash_password(password), role)).fetchone()["id"]


def set_password(conn, username: str, password: str) -> bool:
    check_new_password(password)
    n = conn.execute("UPDATE app_user SET password_hash = %s WHERE username = %s",
                     (hash_password(password), username)).rowcount
    conn.execute("DELETE FROM app_session WHERE user_id = (SELECT id FROM app_user WHERE username = %s)", (username,))
    return n == 1


def disable_user(conn, username: str) -> bool:
    n = conn.execute("UPDATE app_user SET disabled = true WHERE username = %s", (username,)).rowcount
    conn.execute("DELETE FROM app_session WHERE user_id = (SELECT id FROM app_user WHERE username = %s)", (username,))
    return n == 1


def login(conn, username: str, password: str) -> tuple[str, dict] | None:
    """帳密正確且未停用 → 建立登入狀態，回傳（權杖, 使用者）；否則 None。"""
    u = conn.execute("SELECT id, username, role, password_hash, disabled FROM app_user WHERE username = %s",
                     (username,)).fetchone()
    if not u:
        hash_password(password)          # 帳號不存在也花同樣時間，不洩漏帳號是否存在
        return None
    if u["disabled"] or not verify_password(password, u["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO app_session (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                 (_token_hash(token), u["id"], datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)))
    conn.execute("DELETE FROM app_session WHERE expires_at < now()")
    return token, {"id": u["id"], "username": u["username"], "role": u["role"]}


def session_user(conn, token: str | None) -> dict | None:
    if not token:
        return None
    return conn.execute(
        "SELECT u.id, u.username, u.role FROM app_session s JOIN app_user u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.disabled", (_token_hash(token),)).fetchone()


def logout(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM app_session WHERE token_hash = %s", (_token_hash(token),))


def _ask_password() -> str:
    p1 = getpass.getpass("新密碼（輸入時不會顯示）：")
    p2 = getpass.getpass("再輸入一次：")
    if p1 != p2:
        raise SystemExit("兩次輸入不一致")
    return p1


def main(argv: list[str]) -> int:
    import psycopg
    from psycopg.rows import dict_row
    ap = argparse.ArgumentParser(prog="litian.auth", description="工作台帳號管理（只能在主機上執行）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--role", default="reviewer", choices=ROLES)
    s = sub.add_parser("set-password")
    s.add_argument("username")
    d = sub.add_parser("disable")
    d.add_argument("username")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        ensure_schema(conn)
        try:
            if a.cmd == "create-user":
                uid = create_user(conn, a.username, _ask_password(), a.role)
                print(f"已建立帳號 {a.username}（{a.role}，編號 {uid}）")
            elif a.cmd == "set-password":
                print("已更新密碼，原有登入已登出" if set_password(conn, a.username, _ask_password()) else "沒有這個帳號")
            elif a.cmd == "disable":
                print("已停用，原有登入已登出" if disable_user(conn, a.username) else "沒有這個帳號")
            else:
                for u in conn.execute("SELECT username, role, disabled, created_at FROM app_user ORDER BY id").fetchall():
                    print(f"{u['username']:20s} {u['role']:9s} {'停用' if u['disabled'] else '啟用'} {u['created_at']:%Y-%m-%d}")
        except ValueError as e:
            raise SystemExit(str(e))
        except psycopg.errors.UniqueViolation:
            raise SystemExit("帳號已存在")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
