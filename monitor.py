#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Release 监控 -> 邮件通知（APK 直链 + 完整更新日志）
监控：
    FongMi/Release   https://github.com/FongMi/Release/releases
    Null9797/halo-tv https://github.com/Null9797/halo-tv/releases
零第三方依赖，Python 3.8+。

用法：
    python monitor.py            # 检查一次
    python monitor.py --dry-run  # 只打印，不发信、不写 state
    python monitor.py --init     # 只建基线，不发信

环境变量：
    SMTP_USER / SMTP_PASS   QQ 邮箱 + SMTP 授权码（不填只打印）
    SMTP_HOST= smtp.qq.com   SMTP_PORT=465（465=SSL，587=STARTTLS）
    MAIL_TO=888888@qq.com    逗号分隔多人
    MAIL_FROM               默认 = SMTP_USER
    GITHUB_TOKEN            可选，限额 60 -> 5000 次/小时
    INCLUDE_PRERELEASE=1    预发布也算新版本
    SILENT_FIRST_RUN=1      首次只建基线不发信
    INCLUDE_ALL_ASSETS=0    1=除 APK 外的附件也列出来
    LOG_LIMIT=20000         更新日志最多保留多少字符
    PER_PAGE=10             每个仓库取最近多少个 release 比对
"""
from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 配置
WATCH = [
    {"repo": "FongMi/Release",   "alias": "FongMi Release"},
    {"repo": "Null9797/halo-tv", "alias": "halo-tv"},
]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


GH_TOKEN           = os.getenv("GITHUB_TOKEN", "").strip()
INCLUDE_PRERELEASE = env_bool("INCLUDE_PRERELEASE", True)
SILENT_FIRST_RUN   = env_bool("SILENT_FIRST_RUN", True)
INCLUDE_ALL_ASSETS = env_bool("INCLUDE_ALL_ASSETS", False)
PER_PAGE           = max(1, int(os.getenv("PER_PAGE", "10")))
LOG_LIMIT          = max(500, int(os.getenv("LOG_LIMIT", "20000")))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
MAIL_TO   = [x.strip() for x in os.getenv("MAIL_TO", "8888888@qq.com").split(",") if x.strip()]
MAIL_FROM = os.getenv("MAIL_FROM", "").strip() or SMTP_USER


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def human_size(n: int) -> str:
    if not n:
        return "-"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return (f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}")
        f /= 1024.0
    return f"{f:.1f} GB"


# ---------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("repos", {})
                return data
        except Exception as e:
            try:
                STATE_FILE.replace(STATE_FILE.with_suffix(".json.bad"))
            except Exception:
                pass
            log(f"[WARN] state.json 损坏（{e}），已备份并重建")
    return {"repos": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- GitHub API
def gh_api(path: str):
    url = "https://api.github.com" + path
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GH_TOKEN:
        headers["Authorization"] = "Bearer " + GH_TOKEN

    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset") if e.headers else None
                hint = ""
                if reset and str(reset).isdigit():
                    hint = " 重置于 " + datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S")
                raise RuntimeError(f"API 限流/拒绝({e.code}){hint} {detail}")
            if e.code == 404:
                raise RuntimeError(f"仓库不存在或已改名(404) {detail}")
            last_err = f"HTTP {e.code} {detail}"
            if 500 <= e.code < 600:
                time.sleep(3 * attempt)
                continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = e
            time.sleep(3 * attempt)
    raise RuntimeError(f"请求失败: {last_err}")


def pick_release(r: dict) -> dict:
    assets = []
    for a in (r.get("assets") or []):
        assets.append({
            "name": a.get("name") or "",
            "url": a.get("browser_download_url") or "",
            "size": int(a.get("size") or 0),
            "downloads": int(a.get("download_count") or 0),
        })
    # APK 优先，其次按名字
    assets.sort(key=lambda a: (not a["name"].lower().endswith(".apk"), a["name"].lower()))
    return {
        "tag": r.get("tag_name") or "",
        "name": r.get("name") or r.get("tag_name") or "",
        "url": r.get("html_url") or "",
        "published": r.get("published_at") or r.get("created_at") or "",
        "prerelease": bool(r.get("prerelease")),
        "body": (r.get("body") or "").strip(),
        "assets": assets,
    }


# ---------------------------------------------------------------- 邮件内容
def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def html_asset_table(apks, others) -> str:
    def rows(items):
        out = []
        for a in items:
            out.append(
                '<tr>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;word-break:break-all">{esc(a["name"])}</td>'
                f'<td align="right" style="padding:6px 8px;border:1px solid #e5e7eb;white-space:nowrap">{esc(human_size(a["size"]))}</td>'
                f'<td style="padding:6px 8px;border:1px solid #e5e7eb;white-space:nowrap">'
                f'<a href="{esc(a["url"])}" style="color:#2f81f7;text-decoration:none">下载</a></td>'
                '</tr>'
            )
        return "".join(out)

    head = (
        '<tr style="background:#f6f8fa">'
        '<th align="left" style="padding:6px 8px;border:1px solid #e5e7eb;font-weight:600">文件</th>'
        '<th align="right" style="padding:6px 8px;border:1px solid #e5e7eb;font-weight:600">大小</th>'
        '<th align="left" style="padding:6px 8px;border:1px solid #e5e7eb;font-weight:600">直链</th>'
        '</tr>'
    )
    style = 'border-collapse:collapse;width:100%;font-size:13px;font-family:inherit;margin:4px 0 14px'
    out = []
    if apks:
        out.append(f'<div style="font-weight:600;margin:10px 0 4px">APK 直链（{len(apks)} 个）</div>')
        out.append(f'<table style="{style}">{head}{rows(apks)}</table>')
    if others and INCLUDE_ALL_ASSETS:
        out.append(f'<div style="font-weight:600;margin:10px 0 4px;color:#666">其他附件（{len(others)} 个）</div>')
        out.append(f'<table style="{style}">{head}{rows(others)}</table>')
    if not apks and not (others and INCLUDE_ALL_ASSETS):
        out.append('<p style="color:#888;margin:6px 0 12px">该版本 release 未附 APK，请打开详情页查看。</p>')
    return "".join(out)


def build_message(updates):
    parts = []
    for u in updates:
        tags = ", ".join(r["tag"] for r in u["releases"])
        parts.append(f"{u['repo']} {tags}")
    subject = "[GitHub Release] 新版本: " + " | ".join(parts)
    if len(subject) > 180:
        subject = subject[:177] + "..."

    plain = ["检测到 GitHub 仓库发布了新版本。", ""]
    body_html = [
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;'
        'font-size:14px;color:#222;line-height:1.65;max-width:820px">',
        '<h2 style="font-size:18px;margin:0 0 14px">GitHub 新版本发布</h2>',
    ]

    for u in updates:
        plain.append(f"===== {u['alias']}  ({u['repo']}) =====")
        body_html.append(
            f'<h3 style="font-size:16px;margin:22px 0 10px;border-left:4px solid #2f81f7;padding-left:8px">'
            f'{esc(u["alias"])} <span style="color:#888;font-weight:400;font-size:13px">'
            f'({esc(u["repo"])})</span></h3>'
        )

        for r in u["releases"]:
            pre = "（预发布 pre-release）" if r["prerelease"] else ""
            apks = [a for a in r["assets"] if a["name"].lower().endswith(".apk")]
            others = [a for a in r["assets"] if not a["name"].lower().endswith(".apk")]

            # ---- 纯文本
            plain += [
                f"  版本: {r['tag']}{pre}",
                f"  标题: {r['name']}",
                f"  发布: {r['published']}",
                f"  详情: {r['url']}",
            ]
            if apks:
                plain.append(f"  APK 直链（{len(apks)} 个）:")
                for a in apks:
                    plain += [f"    - {a['name']}  ({human_size(a['size'])})", f"      {a['url']}"]
            else:
                plain.append("  APK 直链: 该 release 未附 APK")
            if others and INCLUDE_ALL_ASSETS:
                plain.append(f"  其他附件（{len(others)} 个）:")
                for a in others:
                    plain += [f"    - {a['name']}  ({human_size(a['size'])})", f"      {a['url']}"]

            # ---- HTML
            rows = [
                ("版本", f'<a href="{esc(r["url"])}" style="color:#2f81f7">{esc(r["tag"])}</a>{esc(pre)}'),
                ("标题", esc(r["name"])),
                ("发布时间", esc(r["published"])),
                ("详情页", f'<a href="{esc(r["url"])}" style="color:#2f81f7">{esc(r["url"])}</a>'),
            ]
            table = "".join(
                f'<tr><td style="padding:3px 12px 3px 0;color:#666;white-space:nowrap">{k}</td>'
                f'<td style="padding:3px 0">{v}</td></tr>'
                for k, v in rows
            )
            body_html.append(
                f'<table style="border-collapse:collapse;margin:0 0 6px">{table}</table>'
            )
            body_html.append(html_asset_table(apks, others))

            # ---- 更新日志（完整）
            body_html.append('<div style="font-weight:600;margin:14px 0 4px">更新日志</div>')
            if r["body"]:
                text = r["body"][:LOG_LIMIT]
                cut = len(r["body"]) > LOG_LIMIT
                if cut:
                    text += f"\n\n…（已截断，共 {len(r['body'])} 字符，完整内容见详情页）"
                plain.append("  更新日志:")
                plain += ["    " + ln for ln in text.splitlines()]
                body_html.append(
                    '<pre style="white-space:pre-wrap;word-break:break-word;background:#f6f8fa;'
                    'border:1px solid #e5e7eb;border-radius:6px;padding:10px;font-size:12.5px;'
                    'font-family:Consolas,Menlo,monospace;margin:6px 0 18px">'
                    + esc(text) + "</pre>"
                )
            else:
                plain.append("  更新日志: （该 release 未填写说明）")
                body_html.append(
                    '<p style="color:#888;margin:6px 0 18px">该 release 未填写更新说明。</p>'
                )
            plain.append("")

    plain.append(f"检查时间: {now_iso()}")
    body_html.append(
        f'<p style="color:#999;font-size:12px;margin-top:24px">检查时间: {esc(now_iso())}</p>'
    )
    body_html.append("</div>")
    return subject, "\n".join(plain), "\n".join(body_html)


def send_mail(updates) -> bool:
    subject, plain, body_html = build_message(updates)

    if not SMTP_USER or not SMTP_PASS:
        log("[WARN] 未配置 SMTP_USER / SMTP_PASS，只输出到控制台：")
        print(plain)
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("Release Watcher", "utf-8")), MAIL_FROM))
    msg["To"] = ", ".join(MAIL_TO)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
        try:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(MAIL_FROM, MAIL_TO, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as e:
        log(f"[ERROR] 发信失败: {e}")
        return False

    log(f"[OK] 邮件已发送 -> {', '.join(MAIL_TO)} | {subject}")
    return True


# ---------------------------------------------------------------- 主流程
def main() -> int:
    dry_run = "--dry-run" in sys.argv
    force_init = "--init" in sys.argv

    state = load_state()
    updates = []
    deferred_seen = {}
    immediate_seen = {}

    for item in WATCH:
        repo, alias = item["repo"], item["alias"]
        try:
            raw = gh_api(f"/repos/{repo}/releases?per_page={PER_PAGE}")
        except Exception as e:
            log(f"[WARN] {repo} 查询失败，跳过本轮: {e}")
            continue

        if not isinstance(raw, list) or not raw:
            log(f"[INFO] {repo} 暂无 release 列表")
            continue

        rels = [
            pick_release(r) for r in raw
            if not r.get("draft") and (INCLUDE_PRERELEASE or not r.get("prerelease"))
        ]
        if not rels:
            log(f"[INFO] {repo} 没有符合过滤条件的 release")
            continue

        entry = state["repos"].setdefault(repo, {"seen": [], "initialized": False})
        seen = set(entry.get("seen") or [])
        latest = rels[0]["tag"]

        if force_init or not entry.get("initialized"):
            if force_init or SILENT_FIRST_RUN:
                immediate_seen[repo] = [r["tag"] for r in rels][:200]
                entry["initialized"] = True
                entry["last_check"] = now_iso()
                entry["latest"] = latest
                log(f"[INIT] {repo} 建立基线，当前最新 {latest}（本轮不发信）")
                continue

        fresh = [r for r in rels if r["tag"] not in seen]
        if not fresh:
            entry["last_check"] = now_iso()
            entry["latest"] = latest
            log(f"[--] {repo} 无新版本，最新仍是 {latest}")
            continue

        fresh.sort(key=lambda r: r["published"] or "")
        apk_total = sum(
            1 for r in fresh for a in r["assets"] if a["name"].lower().endswith(".apk")
        )
        log(f"[NEW] {repo} 发现 {len(fresh)} 个新版本: "
            + ", ".join(r["tag"] for r in fresh)
            + f"（APK {apk_total} 个）")
        updates.append({"repo": repo, "alias": alias, "releases": fresh})

        merged = []
        for t in [r["tag"] for r in rels] + list(entry.get("seen") or []):
            if t and t not in merged:
                merged.append(t)
        deferred_seen[repo] = merged[:200]
        entry["initialized"] = True
        entry["last_check"] = now_iso()
        entry["latest"] = latest

    for repo, seen in immediate_seen.items():
        state["repos"].setdefault(repo, {})["seen"] = seen

    if not updates:
        if not dry_run:
            save_state(state)
        log("[DONE] 本轮无新版本")
        return 0

    if dry_run:
        subject, plain, _ = build_message(updates)
        log("[DRY-RUN] 不发信、不写 state，内容如下：")
        print(f"Subject: {subject}\n\n{plain}")
        return 0

    if send_mail(updates):
        for repo, seen in deferred_seen.items():
            state["repos"].setdefault(repo, {})["seen"] = seen
        save_state(state)
        log("[DONE] 已通知并更新 state")
        return 0

    save_state(state)
    log("[DONE] 邮件未发出，state 保留旧 seen，下轮会重试")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)n" in sys.argv
    force_init = "--init" in sys.argv

    state = load_state()
    updates = []
    deferred_seen = {}   # 发送成功后才落盘的 seen
    immediate_seen = {}  # 建基线，立即落盘

    for item in WATCH:
        repo, alias = item["repo"], item["alias"]
        try:
            raw = gh_api(f"/repos/{repo}/releases?per_page={PER_PAGE}")
        except Exception as e:
            log(f"[WARN] {repo} 查询失败，跳过本轮: {e}")
            continue

        if not isinstance(raw, list) or not raw:
            log(f"[INFO] {repo} 暂无 release 列表（或返回空）")
            continue

        rels = [
            pick_release(r) for r in raw
            if not r.get("draft") and (INCLUDE_PRERELEASE or not r.get("prerelease"))
        ]
        if not rels:
            log(f"[INFO] {repo} 没有符合过滤条件的 release")
            continue

        entry = state["repos"].setdefault(repo, {"seen": [], "initialized": False})
        seen = set(entry.get("seen") or [])
        latest = rels[0]["tag"]

        if force_init or not entry.get("initialized"):
            if force_init or SILENT_FIRST_RUN:
                immediate_seen[repo] = [r["tag"] for r in rels][:200]
                entry["initialized"] = True
                entry["last_check"] = now_iso()
                entry["latest"] = latest
                log(f"[INIT] {repo} 建立基线，当前最新 {latest}（本轮不发信）")
                continue

        fresh = [r for r in rels if r["tag"] not in seen]
        if not fresh:
            entry["last_check"] = now_iso()
            entry["latest"] = latest
            log(f"[--] {repo} 无新版本，最新仍是 {latest}")
            continue

        fresh.sort(key=lambda r: r["published"] or "")
        log(f"[NEW] {repo} 发现 {len(fresh)} 个新版本: " + ", ".join(r["tag"] for r in fresh))
        updates.append({"repo": repo, "alias": alias, "releases": fresh})

        merged = []
        for t in [r["tag"] for r in rels] + list(entry.get("seen") or []):
            if t and t not in merged:
                merged.append(t)
        deferred_seen[repo] = merged[:200]
        entry["initialized"] = True
        entry["last_check"] = now_iso()
        entry["latest"] = latest

    # 基线 / 时间戳：总是落盘
    for repo, seen in immediate_seen.items():
        state["repos"].setdefault(repo, {})["seen"] = seen

    if not updates:
        if not dry_run:
            save_state(state)
        log("[DONE] 本轮无新版本")
        return 0

    if dry_run:
        subject, plain, _ = build_message(updates)
        log("[DRY-RUN] 命中的邮件内容如下（不发信、不写 state）：")
        print(f"Subject: {subject}\n\n{plain}")
        return 0

    if send_mail(updates):
        for repo, seen in deferred_seen.items():
            state["repos"].setdefault(repo, {})["seen"] = seen
        save_state(state)
        log("[DONE] 已通知并更新 state")
        return 0

    # 发信失败：不吞掉新版本，state 不写 seen，下一轮重试
    save_state(state)
    log("[DONE] 邮件未发出，state 保留旧 seen，下轮会重试")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
