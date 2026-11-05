# -*- coding: utf-8 -*-
"""
라이팅 아티스트 구인 알림 봇
- 여러 구인 사이트를 확인해서 '라이팅 / lighting' 공고가 새로 올라오면 휴대폰(ntfy 앱)으로 알림을 보냅니다.
- 이미 알린 공고는 seen.json 에 기록해서 다시 알리지 않습니다.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# 1) 검색어 : 사이트 검색창에 넣을 단어
# ─────────────────────────────────────────────
SEARCH_WORDS = ["라이팅", "lighting"]

# ─────────────────────────────────────────────
# 2) 확인할 사이트 목록
#    {q} 자리에 위의 검색어가 들어갑니다.
#    {q} 가 없는 주소는 게시판 첫 페이지를 그대로 확인합니다.
# ─────────────────────────────────────────────
SITES = [
    {"name": "사람인", "url": "https://www.saramin.co.kr/zf_user/search/recruit?searchword={q}&recruitSort=reg_dt&recruitPageCount=40"},
    {"name": "잡코리아", "url": "https://www.jobkorea.co.kr/Search/?stext={q}&tabType=recruit&Ord=RegDtDesc"},
    # 게임잡은 검색 결과를 별도 주소(POST)로 불러옵니다
    {"name": "게임잡", "url": "https://www.gamejob.co.kr/Recruit/_GI_Job_List/", "method": "POST",
     "base": "https://www.gamejob.co.kr/",
     "data": {"order": "3", "page": "1", "condition[searchtype]": "all", "condition[searchstring]": "{q}",
              "condition[menucode]": "", "condition[tabcode]": "1", "direct": "0", "pagesize": "40", "tabcode": "1"},
     "headers": {"X-Requested-With": "XMLHttpRequest",
                 "Referer": "https://www.gamejob.co.kr/Recruit/joblist?menucode=searchtot"}},
    # CGLink는 GitHub 서버 접속을 막아서 제외함
    {"name": "CGLink 외주", "url": "https://cglink.com/index.php?mid=jobsOffer&search_target=title&search_keyword={q}",
     "link_contains": "document_srl=", "cg_site": True},
    # CGLand 구인 게시판 주소는 확인 후 아래에 추가합니다.
    # {"name": "CGLand", "url": "여기에_구인게시판_주소"},
]

# ─────────────────────────────────────────────
# 3) 공고 제목 필터
# ─────────────────────────────────────────────
# 필터 규칙을 바꾸면 숫자를 올립니다 → 기록을 새로 시작하고 현재 공고 목록을 다시 보내줌
FILTER_VERSION = 2

# 제목에 이 단어가 있어야 함
KEYWORD = re.compile(r"라이팅|라이트닝|lighting", re.I)
# 그리고 CG/게임/영상 아트 관련 단어도 함께 있어야 함 (조명회사·글쓰기 공고 제외용)
ART_CONTEXT = re.compile(
    r"아티스트|artist|\bTD\b|3D|CG|VFX|게임|렌더|랜더|합성|룩뎁|look\s*dev|시네마|언리얼|unreal|유니티|unity|"
    r"배경|영상|애니|이펙트|환경|environment|인게임|컴포지|\bcomp|씬|샷|shot|모델링", re.I)
# 이런 단어가 있으면 제외
EXCLUDE = re.compile(
    r"카피\s*라이팅|카피라이터|UX\s*라이팅|UX\s*라이터|테크니컬\s*라이팅|콘텐츠\s*라이팅|컨텐츠\s*라이팅|"
    r"언더\s*라이팅|언더라이터|underwrit|블로그|글쓰기|에세이|논술|copy\s*writ|ux\s*writ|technical\s*writ|"
    r"마케팅\s*라이팅|세일즈\s*라이팅|조명\s*설치|물류|영업", re.I)
# '디에이치라이팅', '라이팅큐브' 처럼 회사 이름에 붙은 라이팅은 무시 (단, '배경라이팅'·'라이팅팀' 등은 인정)
_GLUED_OK_PRE = ("배경", "캐릭터", "인게임", "시네마", "씬", "샷", "게임", "3D", "영상")
_GLUED_OK_SUF = ("아티스트", "팀", "파트", "담당", "TD", "실", "직")


def _strip_company_names(t: str) -> str:
    def rep(m):
        w = m.group(0)
        pre, _, suf = w.partition("라이팅")
        if pre and not pre.endswith(_GLUED_OK_PRE):
            return " "
        if suf and not suf.startswith(_GLUED_OK_SUF):
            return " "
        return w
    return re.sub(r"[가-힣A-Za-z0-9]*라이팅[가-힣0-9]*", rep, t)


NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
REPORT_MODE = os.environ.get("REPORT_MODE", "") == "true"
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 주소에서 공고 번호 역할을 하는 값들 (같은 공고를 한 번만 알리기 위해)
ID_PARAMS = {"rec_idx", "gi_no", "gno", "no", "idx", "wr_id", "id", "seq", "num", "uid", "document_srl", "bo_table"}


def normalize_url(url: str) -> str:
    p = urlparse(url)
    params = parse_qsl(p.query, keep_blank_values=True)
    id_params = [(k, v) for k, v in params if k.lower() in ID_PARAMS]
    query = urlencode(sorted(id_params)) if id_params else urlencode(sorted(params))
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", query, ""))


def is_lighting_title(title: str, cg_site: bool = False) -> bool:
    t = _strip_company_names(title)
    return bool(KEYWORD.search(t)) and (cg_site or bool(ART_CONTEXT.search(t))) and not EXCLUDE.search(title)


def extract_jobs(html: str, base_url: str, link_contains: str = None, cg_site: bool = False):
    """페이지 안의 링크들 중 제목이 라이팅 공고인 것만 골라냅니다."""
    soup = BeautifulSoup(html, "html.parser")
    jobs = {}
    total_links = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        if link_contains and link_contains not in href:
            continue
        total_links += 1
        title = " ".join(a.get_text(" ", strip=True).split()) or (a.get("title") or "").strip()
        if a.get("title") and len(a["title"]) > len(title):
            title = a["title"].strip()
        if len(title) < 4 or len(title) > 200:
            continue
        if not is_lighting_title(title, cg_site):
            continue
        full = urljoin(base_url, href)
        key = normalize_url(full)
        if key not in jobs:
            jobs[key] = {"title": title, "url": full}
    return jobs, total_links


def fetch(url: str, method: str = "GET", data=None, headers=None) -> str:
    h = dict(HEADERS, **(headers or {}))
    if method == "POST":
        r = requests.post(url, data=data, headers=h, timeout=30)
    else:
        r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def notify(title: str, message: str, click: str = None, tags=None, priority: int = 4):
    if not NTFY_TOPIC:
        print("[알림 생략: NTFY_TOPIC 없음]", title, message)
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if click:
        body["click"] = click
        body["actions"] = [{"action": "view", "label": "공고 열기", "url": click}]
    if tags:
        body["tags"] = tags
    try:
        requests.post("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=20)
    except Exception as e:
        print("알림 전송 실패:", e)


def main():
    state = load_state()
    first_run = state is None or state.get("filter_version") != FILTER_VERSION
    if first_run:
        state = {"seen": {}, "last_run_day": "", "filter_version": FILTER_VERSION}

    seen = state["seen"]
    # 새로 추가된 사이트는 처음 한 번은 기존 공고를 '기록만' 하고 요약 알림 1건만 보냄
    known_sites = state.setdefault("known_sites", sorted({v.get("site") for v in seen.values()} | (
        set() if first_run else {s["name"] for s in SITES if s["name"] not in ("CGLink", "CGLink 외주")})))
    added_sites = {}
    new_jobs = []
    report_lines = []

    for site in SITES:
        method = site.get("method", "GET")
        uses_q = "{q}" in site["url"] or "{q}" in json.dumps(site.get("data", {}))
        requests_to_make = []
        for w in (SEARCH_WORDS if uses_q else [None]):
            url = site["url"].replace("{q}", quote(w)) if w else site["url"]
            data = {k: v.replace("{q}", w) if w else v for k, v in site.get("data", {}).items()} or None
            requests_to_make.append((url, data))
        found = {}
        errors = []
        links = 0
        html = ""
        for url, data in requests_to_make:
            try:
                html = fetch(url, method, data, site.get("headers"))
                jobs, n = extract_jobs(html, site.get("base", url), site.get("link_contains"),
                                       site.get("cg_site", False))
                found.update(jobs)
                links += n
            except Exception as e:
                errors.append(str(e)[:80])
            time.sleep(2)

        status = f"{site['name']}: 링크 {links}개 읽음, 라이팅 공고 {len(found)}개"
        if errors:
            status += f" (오류: {errors[0]})"
        elif links == 0 and html:
            # 문제 파악용: 받은 페이지 정보
            soup = BeautifulSoup(html, "html.parser")
            title = (soup.title.get_text(strip=True) if soup.title else "제목없음")[:40]
            hrefs = [a["href"][:60] for a in soup.find_all("a", href=True)][:4]
            status += f" (페이지 '{title}', {len(html)}자, 링크예: {hrefs})"
        print(status)
        report_lines.append(("⚠️ " if errors or links == 0 else "✅ ") + status)

        is_new_site = not first_run and site["name"] not in known_sites
        if is_new_site and errors and not found:
            continue  # 처음인데 오류면 다음 실행에 다시 시도
        for key, job in found.items():
            if key not in seen:
                seen[key] = {"title": job["title"], "site": site["name"],
                             "first_seen": datetime.now(KST).strftime("%Y-%m-%d %H:%M")}
                if is_new_site:
                    added_sites.setdefault(site["name"], []).append(job)
                else:
                    new_jobs.append((site["name"], job))
        if site["name"] not in known_sites:
            known_sites.append(site["name"])

    if first_run:
        preview = "\n".join(f"• [{s}] {j['title']}" for s, j in new_jobs[:15])
        notify("🔦 라이팅 구인 봇 시작!",
               f"지금 올라와 있는 라이팅 공고 {len(new_jobs)}건을 확인했어요. "
               f"앞으로 새 공고가 올라오면 바로 알려드릴게요.\n\n{preview}",
               tags=["tada"])
    else:
        for site_name, job in new_jobs:
            notify(f"🔦 새 라이팅 공고 · {site_name}", job["title"], click=job["url"], tags=["bulb"])
            time.sleep(1)

    for site_name, jobs in added_sites.items():
        preview = "\n".join(f"• {j['title']}" for j in jobs[:15])
        notify(f"🔦 {site_name} 확인 시작",
               f"{site_name}에 지금 올라와 있는 라이팅 공고 {len(jobs)}건을 확인했어요. "
               f"앞으로 새 공고가 올라오면 알려드릴게요.\n\n{preview}", tags=["tada"])

    if REPORT_MODE:
        notify("🩺 봇 점검 결과", "\n".join(report_lines), priority=3)

    # 너무 커지지 않게 오래된 기록 정리 (최근 3000개 유지)
    if len(seen) > 3000:
        items = sorted(seen.items(), key=lambda kv: kv[1].get("first_seen", ""))
        state["seen"] = dict(items[-3000:])

    # 하루 한 번 기록을 남겨서 GitHub가 봇을 '휴면'시키지 않도록 함
    state["last_run_day"] = datetime.now(KST).strftime("%Y-%m-%d")
    save_state(state)
    print(f"새 공고 {len(new_jobs)}건 {'(첫 실행)' if first_run else ''}")


if __name__ == "__main__":
    main()
