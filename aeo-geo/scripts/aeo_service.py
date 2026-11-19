#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AEO 언급률 모니터링 서비스
==========================
『AEO·GEO 실전 핸드북』 9장(DIY 패널)의 구현체.

질문 패널(questions.json)을 4개 AI 엔진(OpenAI·Gemini·Perplexity·Claude)에
웹 검색 모드로 반복 질의하고, 브랜드/경쟁사 언급을 판정해
  - data/runs/{날짜}.json   : 회차별 원시 결과
  - reports/{날짜}_AEO리포트.md : 주간 마크다운 리포트
  - reports/dashboard.html  : 육각형(레이더) 점유율 대시보드
를 생성한다. 표준 라이브러리만 사용(외부 의존성 0).

사용법:
  python3 aeo_service.py run            # 패널 실행 (라이브)
  python3 aeo_service.py run --demo     # API 없이 데모 데이터로 실행
  python3 aeo_service.py report         # 최신 run 기준 리포트 생성
  python3 aeo_service.py dashboard      # 대시보드 HTML 생성
  python3 aeo_service.py all [--demo]   # run + report + dashboard
  python3 aeo_service.py doctor [--live] # 환경 점검 (--live: 엔진별 1회 실호출)
  python3 aeo_service.py health         # 운영 점검 (측정 결손·배포 실패, 비용 0)
  python3 aeo_service.py keyword add    # 핵심 키워드(질문) 대화형 추가 + 오프라인 가이드
  python3 aeo_service.py keyword list   # 패널 키워드/티어/워치/최근 언급률 보기
  python3 aeo_service.py keyword remove # 키워드 삭제 (--id 또는 위저드 선택)

핸드북 원칙(9-2):
  - API 응답 ≠ 소비자 앱 응답 → 절대값이 아니라 '주간 추세'로 해석
  - 같은 질문 3회 반복 → 언급률(%)로 기록 (1회 판정 금지)
  - 판단은 4주 이동평균 기준
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
QUESTIONS_FILE = BASE / "questions.json"

# 현재 패널 이름 (질문 파일 stem). 기본 패널은 "default".
PANEL = "default"


def runs_dir(demo=False):
    """패널별 + 데모/라이브 분리 저장 (추세 오염 방지)."""
    return BASE / "data" / PANEL / ("demo-runs" if demo else "runs")


def report_dir(demo=False):
    d = BASE / "reports" / PANEL
    return d / "demo" if demo else d

ENV_CANDIDATES = [
    BASE / ".env",
]

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "perplexity": "sonar",
    "claude": "claude-haiku-4-5-20251001",
}

ENGINE_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}

# 유료 키가 있으면 우선 사용한다.
# 2026-07-25: 무료 gemini 키가 약 20콜에서 일일 할당량(429)에 걸려 주간 측정이 중간에 끊겼다.
# CeoChat/aeogeo/battery와 동일한 폴백 순서를 따른다.
ENGINE_KEY_ENV_PREFERRED = {
    "gemini": ["GEMINI_API_KEY_PAID", "GEMINI_API_KEY"],
}


def key_env_for(engine):
    """해당 엔진이 실제로 사용할 환경변수 이름. 유료 키가 없으면 기본 키로 폴백한다."""
    for env in ENGINE_KEY_ENV_PREFERRED.get(engine, []):
        if os.environ.get(env):
            return env
    return ENGINE_KEY_ENV[engine]


# ---------------------------------------------------------------- 환경/설정
def load_env():
    """Load project credentials without overriding existing environment variables."""
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def load_config(questions_path=None):
    global PANEL
    qfile = Path(questions_path) if questions_path else QUESTIONS_FILE
    if not qfile.is_absolute():
        qfile = BASE / qfile
    if not qfile.is_file():
        sys.exit(f"[오류] 질문 패널 파일이 없습니다: {qfile}")
    PANEL = "default" if qfile == QUESTIONS_FILE else qfile.stem
    cfg = json.loads(qfile.read_text(encoding="utf-8"))
    cfg.setdefault("brand_aliases", [])
    cfg.setdefault("brand_aliases_weak", [])
    cfg.setdefault("brand_confirm", [])
    cfg.setdefault("brand_exclude", [])
    cfg.setdefault("competitors", {})
    cfg.setdefault("engines", list(DEFAULT_MODELS))
    cfg.setdefault("runs_per_question", 3)
    cfg.setdefault("models", {})
    cfg.setdefault("questions", [])
    if not cfg["brand_aliases"]:
        sys.exit("[오류] questions.json에 brand_aliases가 비어 있습니다.")
    if not cfg["questions"]:
        sys.exit("[오류] questions.json에 questions가 비어 있습니다.")
    return cfg


def model_for(cfg, engine):
    return cfg["models"].get(engine, DEFAULT_MODELS.get(engine, ""))


# ---------------------------------------------------------------- HTTP 공통
class EngineError(Exception):
    pass


_SSL_CTX = None


def ssl_context():
    """검증용 SSL 컨텍스트(1회 생성·캐시).

    macOS python.org 빌드는 시스템 CA 번들이 비어 있어("Install Certificates.command"
    미실행) 모든 HTTPS가 CERTIFICATE_VERIFY_FAILED로 죽는다. 표준 컨텍스트에 CA가 하나도
    없으면 certifi가 설치돼 있을 때 그 번들로 폴백한다(certifi 없으면 그대로 진행 — 하드
    의존성 아님). 이로써 doctor/run(라이브)/주간 cron이 환경변수 없이도 동작한다.
    """
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        try:
            has_ca = bool(ctx.get_ca_certs())
        except Exception:
            has_ca = True  # 확인 불가 시 폴백 시도하지 않음(시스템 신뢰)
        if not has_ca:
            try:
                import certifi
                ctx.load_verify_locations(certifi.where())
            except Exception:
                pass  # certifi 없음 → 표준 동작 유지(키/네트워크면 그대로 실패 노출)
        _SSL_CTX = ctx
    return _SSL_CTX


def http_json(url, payload, headers, timeout=120):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:500]
        raise EngineError(f"HTTP {e.code}: {detail}") from e
    except Exception as e:  # 네트워크/타임아웃 등
        raise EngineError(str(e)) from e


def need_key(engine):
    env = key_env_for(engine)
    key = os.environ.get(env, "")
    if not key:
        raise EngineError(f"{env} 환경변수가 없습니다")
    return key


# ---------------------------------------------------------------- 엔진 어댑터
# 각 어댑터는 {"text": str, "cited_urls": [str], "model": str} 를 반환한다.
# 핸드북 9-2: 반드시 웹 검색 도구를 켠 모드로 호출할 것.

def ask_openai(question, model):
    key = need_key("openai")
    data = http_json(
        "https://api.openai.com/v1/responses",
        {"model": model, "input": question, "tools": [{"type": "web_search"}]},
        {"Authorization": f"Bearer {key}"},
    )
    text, urls = "", []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text += part.get("text", "")
                for ann in part.get("annotations", []):
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        urls.append(ann["url"])
    return {"text": text, "cited_urls": urls, "model": model}


def ask_gemini(question, model):
    key = need_key("gemini")
    data = http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "contents": [{"parts": [{"text": question}]}],
            "tools": [{"google_search": {}}],
        },
        {"x-goog-api-key": key},
    )
    text, urls = "", []
    for cand in data.get("candidates", [])[:1]:
        for part in cand.get("content", {}).get("parts", []):
            text += part.get("text", "")
        gm = cand.get("groundingMetadata", {})
        for chunk in gm.get("groundingChunks", []):
            uri = chunk.get("web", {}).get("uri")
            if uri:
                urls.append(uri)
    return {"text": text, "cited_urls": urls, "model": model}


def ask_perplexity(question, model):
    key = need_key("perplexity")
    data = http_json(
        "https://api.perplexity.ai/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": question}]},
        {"Authorization": f"Bearer {key}"},
    )
    text = ""
    choices = data.get("choices", [])
    if choices:
        text = choices[0].get("message", {}).get("content", "") or ""
    urls = list(data.get("citations", []) or [])
    for sr in data.get("search_results", []) or []:
        if sr.get("url"):
            urls.append(sr["url"])
    return {"text": text, "cited_urls": urls, "model": model}


def ask_claude(question, model):
    key = need_key("claude")
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": question}],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
            ],
        },
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    text, urls = "", []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
            for cit in block.get("citations") or []:
                if cit.get("url"):
                    urls.append(cit["url"])
        elif block.get("type") == "web_search_tool_result":
            for r in block.get("content", []) or []:
                if isinstance(r, dict) and r.get("url"):
                    urls.append(r["url"])
    return {"text": text, "cited_urls": urls, "model": model}


def ask_demo(question, engine, run_index, cfg, date_str=""):
    """API 키 없이 파이프라인을 검증하기 위한 결정적(seed 기반) 가짜 응답."""
    seed = int(hashlib.md5(f"{question}|{engine}|{run_index}|{date_str}".encode()).hexdigest(), 16)
    rng = random.Random(seed)
    brand = cfg["brand_aliases"][0]
    comp_names = list(cfg["competitors"].keys())
    parts = [f"'{question}'에 대한 {engine} 데모 답변입니다."]
    if rng.random() < 0.45:
        parts.append(f"{brand}이(가) 이 분야에서 자주 추천됩니다.")
    for name, aliases in cfg["competitors"].items():
        if rng.random() < 0.5:
            parts.append(f"{aliases[0] if aliases else name}도 후보로 언급됩니다.")
    urls = rng.sample(
        [
            "https://news.example.com/ai-edu",
            "https://blog.example.com/review",
            "https://forum.example.com/thread/42",
            "https://media.example.com/column",
        ],
        k=rng.randint(0, 3),
    )
    return {"text": " ".join(parts), "cited_urls": urls, "model": "demo"}


ENGINE_FUNCS = {
    "openai": ask_openai,
    "gemini": ask_gemini,
    "perplexity": ask_perplexity,
    "claude": ask_claude,
}


# ---------------------------------------------------------------- 언급 판정
def normalize(s):
    """대소문자 무시 + 공백 제거 부분일치용 정규화 (한글 별칭 대응)."""
    return re.sub(r"\s+", "", s.lower())


def find_alias_context(text, alias, exclude_norms=()):
    """원문에서 별칭 위치를 찾아 ±60자 문맥 반환 (제외 패턴이 낀 문맥은 건너뜀)."""
    pattern = r"\s*".join(re.escape(ch) for ch in alias if not ch.isspace())
    if not pattern:
        return None
    for m in re.finditer(pattern, text, re.IGNORECASE):
        ctx = text[max(0, m.start() - 60): m.end() + 60]
        if any(ex in normalize(ctx) for ex in exclude_norms):
            continue  # 오탐(예: 동명의 타사 서적) — 다른 위치 계속 탐색
        return ctx
    return None


def detect_mentions(text, cfg):
    """반환: (브랜드 언급 여부, 경쟁사 목록, 근거 {alias, context} 또는 None)

    별칭은 두 등급이다.
    - brand_aliases: 단독으로 브랜드 귀속이 확실한 고유명사. 그대로 인정한다.
    - brand_aliases_weak: 일반명사와 충돌하는 별칭. 같은 응답 안에 brand_confirm
      중 하나가 함께 있을 때만 인정한다.

    2026-07-25: '인생OS'/'LifeOS'가 일반 개념(Life OS 프레임워크)과 동명 해외 앱까지
    잡아내 언급률이 부풀려졌다. 브랜드가 특정되지 않은 언급은 AEO 자산이 아니므로
    확증 요건을 둔다.
    """
    norm = normalize(text)
    exclude_norms = [normalize(e) for e in cfg.get("brand_exclude", []) if e]
    evidence = None
    for alias in cfg["brand_aliases"]:
        ctx = find_alias_context(text, alias, exclude_norms)
        if ctx:
            evidence = {"alias": alias, "context": ctx}
            break
    if evidence is None:
        weak = [a for a in cfg.get("brand_aliases_weak", []) if a]
        confirm = [c for c in (cfg.get("brand_confirm") or cfg["brand_aliases"]) if c]
        if weak and any(normalize(c) in norm for c in confirm):
            for alias in weak:
                ctx = find_alias_context(text, alias, exclude_norms)
                if ctx:
                    evidence = {"alias": alias, "context": ctx, "confirmed_by": "brand_confirm"}
                    break
    competitors = []
    for name, aliases in cfg["competitors"].items():
        terms = [name] + list(aliases or [])
        if any(normalize(t) in norm for t in terms if t):
            competitors.append(name)
    return evidence is not None, competitors, evidence


# ---------------------------------------------------------------- 패널 실행
def cmd_run(cfg, demo=False, date_str=None):
    today = date_str or datetime.date.today().isoformat()
    out_dir = runs_dir(demo)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{today}.json"

    engines = list(cfg["engines"])
    if not demo:
        # 키 없는 엔진은 사전 제외 (경고 후 진행)
        for engine in list(engines):
            env = key_env_for(engine)
            if not os.environ.get(env):
                print(f"[경고] {engine}: {env} 없음 → 이번 회차에서 제외")
                engines.remove(engine)
        if not engines:
            sys.exit("[오류] 사용 가능한 엔진이 없습니다. doctor로 키를 점검하세요.")

    records = []
    consecutive_errors = {e: 0 for e in engines}  # 3회 연속 오류 → 해당 엔진 중단
    n_total = len(cfg["questions"]) * len(engines) * cfg["runs_per_question"]
    done = 0
    print(f"[run] {today} | 질문 {len(cfg['questions'])}개 × 엔진 {len(engines)}개 "
          f"× {cfg['runs_per_question']}회 = 총 {n_total}콜 ({'데모' if demo else '라이브'})")

    for q in cfg["questions"]:
        for engine in engines:
            for run_index in range(cfg["runs_per_question"]):
                done += 1
                if consecutive_errors[engine] >= 3:
                    continue  # 서킷브레이커: 죽은 엔진에 콜 낭비 금지
                try:
                    if demo:
                        res = ask_demo(q["text"], engine, run_index, cfg, today)
                    else:
                        res = ENGINE_FUNCS[engine](q["text"], model_for(cfg, engine))
                    brand, comps, evidence = detect_mentions(res["text"], cfg)
                    records.append({
                        "question_id": q["id"],
                        "engine": engine,
                        "run_index": run_index,
                        "brand_mentioned": brand,
                        "match_evidence": evidence,
                        "competitors_mentioned": comps,
                        "cited_urls": res["cited_urls"][:20],
                        "raw_excerpt": res["text"][:200],
                        "model": res.get("model", ""),
                    })
                    mark = "●" if brand else "○"
                    consecutive_errors[engine] = 0
                    print(f"  [{done}/{n_total}] {q['id']} × {engine} #{run_index+1} {mark}")
                except EngineError as e:
                    records.append({
                        "question_id": q["id"],
                        "engine": engine,
                        "run_index": run_index,
                        "error": str(e)[:300],
                    })
                    consecutive_errors[engine] += 1
                    print(f"  [{done}/{n_total}] {q['id']} × {engine} #{run_index+1} ✗ {str(e)[:80]}")
                    if consecutive_errors[engine] >= 3:
                        print(f"  [중단] {engine}: 3회 연속 오류 → 이번 회차에서 남은 콜 건너뜀")
                if not demo:
                    time.sleep(1)  # 레이트리밋 완충

    payload = {
        "date": today,
        "mode": "demo" if demo else "live",
        "brand_aliases": cfg["brand_aliases"],
        "engines": engines,
        "runs_per_question": cfg["runs_per_question"],
        "questions": {q["id"]: q["text"] for q in cfg["questions"]},
        "records": records,
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    errs = sum(1 for r in records if "error" in r)
    print(f"[run] 저장: {out_file} (기록 {len(records)}건, 오류 {errs}건)")

    # 리다이렉트 해독은 run 직후가 유일한 기회다(수 주 내 만료). 실패해도 측정은 유효.
    if not demo:
        try:
            vurls = [u for r in records for u in r.get("cited_urls", [])
                     if is_redirect_url(u)]
            n_ok, n_dead, n_fail = resolve_redirects(vurls)
            if vurls:
                print(f"[run] 리다이렉트 해독: 성공 {n_ok} / 만료 {n_dead} / "
                      f"실패 {n_fail} (대상 {len(set(vurls))}개)")
        except Exception as e:
            print(f"[경고] 리다이렉트 해독 건너뜀: {e}")
    return out_file


# ---------------------------------------------------------------- 집계
def load_runs(demo=False):
    """날짜순으로 정렬된 모든 run 파일 로드."""
    runs = []
    d = runs_dir(demo)
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                runs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return runs


def domain_of(url):
    """인용 URL에서 매체 도메인을 추출 (www./m. 접두 제거).

    리포트 §6의 '내 콘텐츠가 실려야 할 곳'은 개별 URL보다 **도메인(매체)** 단위가
    실행에 직결된다. 같은 허브가 여러 경로로 인용되면(예: winspec.co.kr 의
    /community/... 와 /support/...) URL 단위 집계에서는 분산돼 허브임이 가려진다.
    도메인으로 묶어 등재·제휴 우선순위를 드러낸다. 도메인 추출 실패 시 빈 문자열.
    """
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


# ---------------------------------------------------------- 리다이렉트 해독
# gemini 그라운딩 인용은 전부 vertexaisearch.cloud.google.com 리다이렉트로 온다.
# 그대로 집계하면 존재하지 않는 '매체'가 허브 1위를 차지해 §6이 무의미해진다
# (2026-08-24 sample-b: 242/600건). 리다이렉트는 수 주 내 만료되므로(2026-06 회차
# URL은 이미 404) 해독의 유일한 기회는 run 직후다. 결과는 캐시에 누적하고,
# 죽은 URL은 null로 기록해 재시도를 막는다.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)
REDIRECT_CACHE_FILE = BASE / "data" / "_redirect_cache.json"
REDIRECT_UNRESOLVED = "(구글 리다이렉트 · 미해독)"

_redirect_cache = None


def is_redirect_url(url):
    return any(h in url for h in REDIRECT_HOSTS)


def load_redirect_cache():
    global _redirect_cache
    if _redirect_cache is None:
        try:
            _redirect_cache = json.loads(
                REDIRECT_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _redirect_cache = {}
    return _redirect_cache


def resolve_redirects(urls, timeout=8, max_workers=8):
    """미해독 리다이렉트 URL을 실제 URL로 풀어 캐시에 누적 저장한다.

    반환: (해독 성공 수, 만료(404 등) 수, 네트워크 실패 수).
    네트워크 실패는 캐시에 남기지 않아 다음 기회에 재시도된다.
    """
    import concurrent.futures

    cache = load_redirect_cache()
    todo = sorted({u for u in urls if is_redirect_url(u) and u not in cache})
    if not todo:
        return (0, 0, 0)

    def head(u):
        req = urllib.request.Request(
            u, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
                return (u, final if not is_redirect_url(final) else None, None)
        except urllib.error.HTTPError as e:
            final = e.geturl() or ""
            if final and not is_redirect_url(final):
                return (u, final, None)  # 리다이렉트는 풀렸고 목적지가 4xx인 경우
            return (u, None, None)       # 리다이렉트 자체가 만료(404)
        except Exception as e:
            return (u, None, e)          # 네트워크 실패 → 캐시 기록 안 함

    ok = dead = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for u, final, err in ex.map(head, todo):
            if err is not None:
                fail += 1
            elif final:
                cache[u] = final
                ok += 1
            else:
                cache[u] = None
                dead += 1
    try:
        REDIRECT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        REDIRECT_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception as e:
        print(f"[경고] 리다이렉트 캐시 저장 실패: {e}")
    return (ok, dead, fail)


def aggregate(run):
    """단일 run에 대한 집계: 엔진별/질문별 언급률, 경쟁사, 인용 URL·도메인.

    인용 URL은 리다이렉트 캐시로 해독한 실제 URL 기준으로 센다. 해독 못한
    리다이렉트는 실제 매체인 척 순위에 오르지 못하게 별도 버킷으로 묶는다.
    """
    rcache = load_redirect_cache()
    ok = [r for r in run["records"] if "error" not in r]
    by_engine, by_question, comp_counts, urls, domains = {}, {}, {}, {}, {}
    engine_cites = {}
    for r in ok:
        e, q = r["engine"], r["question_id"]
        by_engine.setdefault(e, [0, 0])
        by_question.setdefault(q, [0, 0])
        by_engine[e][1] += 1
        by_question[q][1] += 1
        if r["brand_mentioned"]:
            by_engine[e][0] += 1
            by_question[q][0] += 1
        for c in r.get("competitors_mentioned", []):
            comp_counts[c] = comp_counts.get(c, 0) + 1
        for u in r.get("cited_urls", []):
            engine_cites[e] = engine_cites.get(e, 0) + 1
            if is_redirect_url(u):
                target = rcache.get(u)
                if not target:
                    domains[REDIRECT_UNRESOLVED] = domains.get(REDIRECT_UNRESOLVED, 0) + 1
                    continue
                u = target
            urls[u] = urls.get(u, 0) + 1
            host = domain_of(u)
            if host:
                domains[host] = domains.get(host, 0) + 1

    def rate(pair):
        return round(100.0 * pair[0] / pair[1], 1) if pair[1] else None

    # 엔진 커버리지. 서킷브레이커(3회 연속 오류)로 끊긴 엔진은 by_engine에서 통째로 사라진다.
    # 그대로 두면 3엔진 회차와 4엔진 회차가 같은 표에서 비교돼 실적 변화로 오독된다
    # (2026-08-03 sample-b: claude 0/18인데 리포트에는 3엔진만 정상 표기됐다).
    expected = len(run.get("questions", {})) * run.get("runs_per_question", 1)
    engine_calls = {e: by_engine.get(e, [0, 0])[1]
                    for e in run.get("engines", sorted(by_engine))}
    degraded = {e: n for e, n in engine_calls.items() if n < expected}

    # 별칭 정의. run JSON은 실행 시점의 brand_aliases를 함께 저장한다. 이 정의가 바뀌면
    # 같은 이름의 지표라도 세는 대상이 달라지므로, 엔진 커버리지와 똑같이 비교 불가 구간이
    # 생긴다 (2026-08-08 sample-a: '인생OS'·'LifeOS'가 강별칭 → 약별칭으로 재분류되며
    # 7월 8.3~9.7%가 0%로 떨어졌는데, 실적 하락이 아니라 정의 변경이었다).
    total = [sum(v[0] for v in by_engine.values()), sum(v[1] for v in by_engine.values())]
    return {
        "date": run["date"],
        "mode": run.get("mode", "?"),
        "alias_set": tuple(sorted(run.get("brand_aliases", []))),
        "total_rate": rate(total),
        "total_calls": total[1],
        "errors": len(run["records"]) - len(ok),
        "engine_calls": engine_calls,
        "engine_expected": expected,
        "engines_degraded": degraded,
        "engine_rates": {e: rate(v) for e, v in sorted(by_engine.items())},
        "question_rates": {q: rate(v) for q, v in sorted(by_question.items())},
        "competitor_counts": dict(sorted(comp_counts.items(), key=lambda x: -x[1])),
        "competitor_total": total[1],
        "cited_urls": dict(sorted(urls.items(), key=lambda x: -x[1])),
        "cited_domains": dict(sorted(domains.items(), key=lambda x: -x[1])),
        # 엔진별 인용 수. 국내 니치 질문에서 인용 0인 엔진(예: gemini 일부 질문)은
        # 그라운딩이 약하다는 신호 — 언급률과 별개로 §6에서 보여준다.
        "engine_citations": {e: engine_cites.get(e, 0) for e in sorted(by_engine)},
    }


def moving_average(aggs, window=4):
    """최근 window회(주 1회 실행 가정 = 4주) 이동평균.

    호출부에서 **별칭 정의가 같은 회차만** 넘겨야 한다. 정의가 바뀐 회차를 섞으면
    평균 자체가 존재하지 않는 기준선을 만들어낸다(아래 comparable_aggs 참고).
    """
    recent = [a["total_rate"] for a in aggs[-window:] if a["total_rate"] is not None]
    return round(sum(recent) / len(recent), 1) if recent else None


def comparable_aggs(aggs, cur):
    """현재 회차와 별칭 정의가 같아 세로 비교가 성립하는 회차만 남긴다.

    정의가 바뀐 시점보다 앞선 회차는 '같은 지표'가 아니다. 지표 이름이 같다고
    이어 붙이면 없던 하락·회복이 만들어진다(핸드북 11장 측정 착시의 별칭 판).
    """
    cut = 0
    for i, a in enumerate(aggs):
        if a.get("alias_set") != cur.get("alias_set"):
            cut = i + 1
    return aggs[cut:]


# ---------------------------------------------------------------- 리포트
def cmd_report(cfg, demo=False):
    runs = load_runs(demo)
    if not runs:
        sys.exit("[오류] 실행 결과가 없습니다. 먼저 `run`을 실행하세요.")
    aggs = [aggregate(r) for r in runs]
    cur, prev = aggs[-1], (aggs[-2] if len(aggs) > 1 else None)
    run = runs[-1]
    # 이동평균은 별칭 정의가 같은 구간에서만 낸다. 정의가 바뀐 회차를 섞으면
    # 존재하지 않는 기준선이 생긴다(예: sample-a 4주 이동평균 4.5% — 실제 확증 언급은 0건).
    same_def = comparable_aggs(aggs, cur)
    ma4 = moving_average(same_def)
    def_break = len(same_def) < len(aggs)

    def delta(now, before):
        if now is None or before is None:
            return "—"
        d = round(now - before, 1)
        return f"{'+' if d >= 0 else ''}{d}%p"

    # 워치(모니터링 경보) — 패널에 watch=true로 표시된 질문 중 0%이거나 급락(≤ -20%p)한 것.
    watch_ids = {q["id"] for q in cfg.get("questions", []) if q.get("watch")}
    DROP_PP = 20.0
    watch_alerts = []
    for qid in sorted(watch_ids):
        r = cur["question_rates"].get(qid)
        if r is None:
            continue
        p = prev["question_rates"].get(qid) if prev else None
        qtext = run["questions"].get(qid, qid)
        if r == 0:
            watch_alerts.append((qid, qtext, r, p, "0% — AI가 미언급"))
        elif p is not None and (r - p) <= -DROP_PP:
            watch_alerts.append((qid, qtext, r, p, f"급락 {delta(r, p)}"))

    lines = []
    lines.append(f"# AEO 주간 리포트 — {cur['date']}")
    lines.append("")
    lines.append(f"> 브랜드: **{cfg['brand_aliases'][0]}** · 모드: {cur['mode']} · "
                 f"총 {cur['total_calls']}콜 (오류 {cur['errors']}건)")
    lines.append("> 해석 원칙(핸드북 9-2): 절대값이 아니라 **주간 추세**로 볼 것. 판단은 4주 이동평균.")
    lines.append("")
    # 엔진 커버리지 경고. aggregate()가 engines_degraded를 계산해두고도 어디서도 출력하지
    # 않아, 2026-08-03 이후 claude가 크레딧 소진으로 완전히 죽은 채 3엔진 측정이
    # 4엔진 회차와 나란히 표에 올라가고 있었다(리포트 표기는 "오류 3건"뿐).
    # 엔진 구성이 바뀐 회차는 증감을 실적 변화로 읽으면 안 되므로 최상단에 세운다.
    if cur["engines_degraded"]:
        lines.append("## ⚠ 엔진 커버리지 경고 — 이 회차는 비교 기준이 다르다")
        lines.append("")
        lines.append(f"엔진별 기대 콜은 {cur['engine_expected']}콜"
                     f"(질문 {len(run.get('questions', {}))}개 × {run.get('runs_per_question', 1)}회)이다.")
        lines.append("")
        lines.append("| 엔진 | 성공 콜 | 기대 콜 | 상태 |")
        lines.append("|---|---|---|---|")
        for e, n in cur["engine_calls"].items():
            if n >= cur["engine_expected"]:
                continue
            lines.append(f"| {e} | {n} | {cur['engine_expected']} | "
                         f"{'**전면 실패 — 집계 제외**' if n == 0 else '부분 실패'} |")
        lines.append("")
        dead = [e for e, n in cur["engines_degraded"].items() if n == 0]
        if dead:
            lines.append(f"> `{', '.join(dead)}`는 이번 회차 집계에서 통째로 빠졌다. "
                         "§2 엔진표와 §7 추이의 전회 대비 증감은 **엔진 구성이 다른 값끼리의 비교**다. "
                         "실적 변화로 읽지 말고, 키·크레딧을 복구한 뒤 재측정해 추세를 이어야 한다.")
        lines.append("")
    # 별칭 정의 경고. 엔진 커버리지와 같은 급의 비교 불가 사유인데 그동안 아무 표시가 없었다.
    if def_break:
        old = aggs[len(aggs) - len(same_def) - 1]
        added = [a for a in cur["alias_set"] if a not in old["alias_set"]]
        removed = [a for a in old["alias_set"] if a not in cur["alias_set"]]
        lines.append("## ⚠ 별칭 정의 변경 — 이전 회차와 같은 지표가 아니다")
        lines.append("")
        lines.append(f"`{old['date']}`까지와 `{same_def[0]['date']}`부터는 **브랜드로 세는 단어가 다르다.**")
        lines.append("")
        lines.append("| 구분 | 별칭 |")
        lines.append("|---|---|")
        if removed:
            lines.append(f"| 제외됨 | {', '.join(f'`{a}`' for a in removed)} |")
        if added:
            lines.append(f"| 추가됨 | {', '.join(f'`{a}`' for a in added)} |")
        lines.append(f"| 현재 기준 | {', '.join(f'`{a}`' for a in cur['alias_set'])} |")
        lines.append("")
        weak = cfg.get("brand_aliases_weak") or []
        if weak:
            lines.append(f"> 제외된 단어 중 {', '.join(f'`{a}`' for a in weak)}는 일반명사와 겹쳐 "
                         "**확증어와 함께 나올 때만** 브랜드로 센다(`brand_confirm`). "
                         "확증 없는 등장은 남의 브랜드이거나 일반 개념이므로 우리 AEO 자산이 아니다.")
            lines.append("")
        lines.append(f"> **정의 변경 이전 회차와의 증감·이동평균은 무효다.** 4주 이동평균은 "
                     f"현재 정의 구간({same_def[0]['date']} 이후 {len(same_def)}회)에서만 계산했다. "
                     "§7 추이표의 이전 구간 행은 `정의 이전`으로 표시된다.")
        lines.append("")
    if watch_ids:
        lines.append("## 🔔 워치 키워드 경보")
        lines.append("")
        if watch_alerts:
            lines.append("| 키워드 | 현재 | 전회 | 상태 |")
            lines.append("|---|---|---|---|")
            for _qid, qtext, r, p, reason in watch_alerts:
                lines.append(f"| 👁 {qtext} | {r}% | {p if p is not None else '—'}"
                             f"{'%' if p is not None else ''} | **{reason}** |")
            lines.append("")
            lines.append("> 보강 방법: 핸드북 8장 — `prompts/GEO_리라이트_프롬프트.md`로 해당 질문 답변 글 보강 후 4주 관찰.")
        else:
            lines.append(f"- 워치 {len(watch_ids)}개 정상 (0%·급락 없음) ✅")
        lines.append("")
    lines.append("## 1. 총괄")
    lines.append("")
    lines.append("| 지표 | 값 | 전회 대비 |")
    lines.append("|---|---|---|")
    # 전회가 다른 정의로 측정됐다면 증감 자체가 성립하지 않는다.
    prev_ok = prev is not None and prev.get("alias_set") == cur.get("alias_set")
    lines.append(f"| 전체 언급률 | **{cur['total_rate']}%** | "
                 f"{delta(cur['total_rate'], prev['total_rate']) if prev_ok else '비교 불가(정의 변경)'} |")
    lines.append(f"| 4주 이동평균 | {ma4}% | "
                 f"{'현 정의 %d회 기준' % len(same_def) if def_break else '—'} |")
    lines.append("")
    lines.append("## 2. 엔진별 언급률 (육각형 원데이터)")
    lines.append("")
    lines.append("| 엔진 | 언급률 | 전회 | 증감 |")
    lines.append("|---|---|---|---|")
    for e, r in cur["engine_rates"].items():
        p = prev["engine_rates"].get(e) if prev else None
        lines.append(f"| {e} | {r}% | {p if p is not None else '—'}"
                     f"{'%' if p is not None else ''} | {delta(r, p)} |")
    # 전면 실패한 엔진은 engine_rates에서 아예 사라져 행이 없어진다. 없는 행은 눈에 띄지
    # 않으므로 명시적으로 남긴다 — 전회 값이 있으면 그 값도 함께 보여 낙폭을 감추지 않는다.
    for e in cur["engine_calls"]:
        if e in cur["engine_rates"]:
            continue
        p = prev["engine_rates"].get(e) if prev else None
        lines.append(f"| {e} | **측정 실패** | {p if p is not None else '—'}"
                     f"{'%' if p is not None else ''} | — |")
    lines.append("")
    lines.append("## 3. 질문별 언급률")
    lines.append("")
    lines.append("| 질문 | 언급률 |")
    lines.append("|---|---|")
    for qid, r in cur["question_rates"].items():
        qtext = run["questions"].get(qid, qid)
        eye = "👁 " if qid in watch_ids else ""
        lines.append(f"| {eye}{qtext} | {r}% |")
    lines.append("")

    zero_qs = [(q, run["questions"].get(q, q)) for q, r in cur["question_rates"].items() if r == 0]
    lines.append("## 4. 언급률 0% 질문 — 콘텐츠 보강 후보")
    lines.append("")
    if zero_qs:
        for qid, q in zero_qs:
            eye = "👁 " if qid in watch_ids else ""
            lines.append(f"- {eye}{q}")
        lines.append("")
        lines.append("> 보강 방법: 핸드북 8장 — 통계 +41% / 출처 인용 최대 +115% / 인용문 +28% (KDD 2024).")
    else:
        lines.append("- 없음 🎉")
    lines.append("")

    lines.append("## 5. 경쟁사 언급 횟수")
    lines.append("")
    if cur["competitor_counts"]:
        lines.append("| 경쟁사 | 언급 횟수 (전체 응답 대비) |")
        lines.append("|---|---|")
        for c, n in cur["competitor_counts"].items():
            lines.append(f"| {c} | {n}회 / {cur['competitor_total']}응답 |")
    else:
        lines.append("- 경쟁사 언급 없음")
    lines.append("")

    prev_urls = set(prev["cited_urls"]) if prev else set()
    new_urls = [u for u in cur["cited_urls"] if u not in prev_urls]
    prev_domains = set(prev["cited_domains"]) if prev else set()
    lines.append("## 6. 인용 매체 — 내 콘텐츠가 실려야 할 곳")
    lines.append("")
    lines.append("AI가 답변 근거로 실제 인용한 매체(핸드북 6-3). "
                 "**도메인 기준 = 등재·제휴 우선순위** (반복 인용 허브가 곧 영향력 있는 채널).")
    lines.append("")
    # 엔진별 인용 수 — 언급률과 별개로, 어느 엔진이 이 질문 공간을 실제 웹 근거로
    # 답하는지(그라운딩 강약)를 보여준다. 인용 0인 엔진에는 콘텐츠 등재가 안 먹힌다.
    if cur.get("engine_citations"):
        parts = [f"{e} {n}건" for e, n in cur["engine_citations"].items()]
        lines.append(f"- 엔진별 인용 수: {' · '.join(parts)} — 인용 0건인 엔진은 "
                     "웹 근거 없이 답하므로 매체 등재로는 닿을 수 없다.")
    unresolved = cur["cited_domains"].get(REDIRECT_UNRESOLVED)
    if unresolved:
        lines.append(f"- gemini 인용은 구글 리다이렉트를 실제 매체로 해독해 집계했다. "
                     f"미해독 {unresolved}건은 `{REDIRECT_UNRESOLVED}` 행으로 분리 "
                     "(리다이렉트 만료로 복구 불가한 과거 회차분).")
    lines.append("")
    lines.append("### 6-1. 인용 허브 — 도메인 순위")
    lines.append("")
    if cur["cited_domains"]:
        lines.append("| 순위 | 매체(도메인) | 인용 횟수 |")
        lines.append("|---|---|---|")
        for i, (h, n) in enumerate(list(cur["cited_domains"].items())[:12], 1):
            flag = " 🆕" if h not in prev_domains else ""
            lines.append(f"| {i} | {h}{flag} | {n}회 |")
    else:
        lines.append("- (인용 도메인 없음)")
    lines.append("")
    lines.append("### 6-2. 개별 인용 URL (상위 15)")
    lines.append("")
    for u, n in list(cur["cited_urls"].items())[:15]:
        flag = " 🆕" if u in new_urls else ""
        lines.append(f"- {u} ({n}회){flag}")
    if not cur["cited_urls"]:
        lines.append("- (인용 URL 없음)")
    lines.append("")

    lines.append("## 7. 추이")
    lines.append("")
    # 엔진 열을 함께 둔다. 엔진 수가 바뀐 회차를 모르고 세로로 읽으면 커버리지 축소가
    # 언급률 변동으로 오독된다(핸드북 11장 측정 착시).
    # 별칭 열도 함께 둔다. 정의가 바뀐 회차는 세로로 이어 읽으면 안 된다.
    lines.append("| 회차 | 전체 언급률 | 측정 엔진 | 별칭 정의 |")
    lines.append("|---|---|---|---|")
    for a in aggs[-8:]:
        n_ok = sum(1 for n in a["engine_calls"].values() if n > 0)
        n_cfg = len(a["engine_calls"]) or n_ok
        same = a.get("alias_set") == cur.get("alias_set")
        lines.append(f"| {a['date']} | {a['total_rate']}% | "
                     f"{n_ok}/{n_cfg}{' ⚠' if a['engines_degraded'] else ''} | "
                     f"{'현행' if same else '**정의 이전 — 비교 불가**'} |")
    lines.append("")
    lines.append("---")
    lines.append(f"*생성: aeo_service.py · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} · "
                 "측정 한계: API 응답은 소비자 앱과 다를 수 있음(개인화·모델 차이)*")

    rdir = report_dir(demo)
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / f"{cur['date']}_AEO리포트.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] 저장: {out}")

    summary = (f"📊 AEO 주간({cur['date']}) 총 언급률 {cur['total_rate']}% "
               f"({delta(cur['total_rate'], prev['total_rate'] if prev else None)}), "
               f"보강 후보 질문 {len(zero_qs)}개")
    if watch_alerts:
        head = watch_alerts[0]
        more = f" 외 {len(watch_alerts) - 1}건" if len(watch_alerts) > 1 else ""
        summary += f"\n🔔 워치 경보: {head[1]} {head[4]}{more}"
    if not demo:
        notify_telegram(summary)
    return out


def notify_telegram(text):
    """TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 가 있으면 3줄 요약 전송 (없으면 건너뜀)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        http_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat, "text": text},
            {},
            timeout=15,
        )
        print("[telegram] 요약 전송 완료")
    except EngineError as e:
        print(f"[telegram] 전송 실패(무시): {e}")


# ---------------------------------------------------------------- 대시보드
def cmd_dashboard(cfg, demo=False):
    runs = load_runs(demo)
    if not runs:
        sys.exit("[오류] 실행 결과가 없습니다. 먼저 `run`을 실행하세요.")
    aggs = [aggregate(r) for r in runs]
    history = [
        {
            "date": a["date"],
            "total": a["total_rate"],
            "engines": a["engine_rates"],
            "questions": a["question_rates"],
            "competitors": a["competitor_counts"],
            "competitor_total": a["competitor_total"],
            "urls": list(a["cited_urls"].keys())[:10],
            "domains": list(a["cited_domains"].items())[:10],
            "mode": a["mode"],
            # 레이더는 cur.engines의 축만 그린다. 끊긴 엔진은 축째로 사라져 사각형이
            # 삼각형이 되고, 전회 폴리곤도 같은 축에만 투영돼 낙폭이 보이지 않는다.
            "engine_calls": a["engine_calls"],
            "engine_expected": a["engine_expected"],
            "degraded": a["engines_degraded"],
        }
        for a in aggs
    ]
    qtexts = runs[-1]["questions"]
    # 이동평균은 리포트와 같은 기준(현재 별칭 정의 구간)으로 낸다. 두 산출물이 다른
    # 수치를 내놓으면 어느 쪽이 맞는지 운영자가 판단할 수 없다.
    same_def = comparable_aggs(aggs, aggs[-1])
    payload = {
        "brand": cfg["brand_aliases"][0],
        "ma4": moving_average(same_def),
        "def_break_from": same_def[0]["date"] if len(same_def) < len(aggs) else None,
        "history": history,
        "question_texts": qtexts,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    html = DASHBOARD_TEMPLATE.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    rdir = report_dir(demo)
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"[dashboard] 저장: {out}")
    return out


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AEO 점유율 대시보드</title>
<style>
  :root{--bg:#0f1420;--card:#1a2232;--ink:#e8edf6;--sub:#8b97ad;--acc:#4da3ff;--good:#3ddc84;--bad:#ff6b6b;--line:#2a3550}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:'Apple SD Gothic Neo','Pretendard',sans-serif;padding:24px;max-width:1080px;margin:0 auto}
  h1{font-size:22px;margin-bottom:4px} .sub{color:var(--sub);font-size:13px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px} @media(max-width:760px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
  .card h2{font-size:15px;margin-bottom:12px;color:var(--acc)}
  .kpi{display:flex;gap:24px;flex-wrap:wrap}
  .kpi .item{flex:1;min-width:120px}
  .kpi .num{font-size:30px;font-weight:800} .kpi .lbl{color:var(--sub);font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--sub);font-weight:600;font-size:12px}
  .up{color:var(--good)} .down{color:var(--bad)}
  svg text{fill:var(--sub);font-size:11px}
  .note{color:var(--sub);font-size:11px;margin-top:10px;line-height:1.6}
  .bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--acc)}
</style>
</head>
<body>
<h1 id="title">AEO 점유율 대시보드</h1>
<div class="sub" id="meta"></div>
<div class="card" style="margin-bottom:16px"><div class="kpi" id="kpi"></div></div>
<div class="grid">
  <div class="card"><h2>엔진별 언급률 (육각형)</h2><div id="radar"></div>
    <div class="note">영상 속 "AI 모델별 답변 점유율 육각형"의 자기주권 버전. 축 = 엔진, 값 = 언급률%.</div></div>
  <div class="card"><h2>주간 추이 (전체 언급률)</h2><div id="trend"></div>
    <div class="note">핸드북 원칙: 1회 급등/급락에 반응하지 말고 4주 이동평균으로 판단.</div></div>
  <div class="card"><h2>질문별 언급률 (최신)</h2><table id="qtable"></table></div>
  <div class="card"><h2>경쟁사 언급 (최신)</h2><table id="ctable"></table>
    <h2 style="margin-top:18px">인용 허브 (도메인 순위)</h2><div id="urls" style="font-size:12px;line-height:1.9;color:var(--sub)"></div></div>
</div>
<script>
const DATA = /*__DATA__*/null;
const hist = DATA.history, cur = hist[hist.length-1], prev = hist.length>1?hist[hist.length-2]:null;
document.getElementById('title').textContent = `AEO 점유율 대시보드 — ${DATA.brand}`;
document.getElementById('meta').textContent = `최신 측정 ${cur.date} (${cur.mode}) · 생성 ${DATA.generated} · 절대값이 아닌 주간 추세로 해석하세요`;

function deltaTag(now, before){
  if(now==null||before==null) return '<span class="sub">—</span>';
  const d = +(now-before).toFixed(1);
  return `<span class="${d>=0?'up':'down'}">${d>=0?'+':''}${d}%p</span>`;
}
// KPI
// 측정 엔진 수는 성공/설정으로 적는다. 3만 찍으면 원래 3엔진 패널인지 하나가 죽은
// 4엔진 패널인지 구분되지 않아, 커버리지 축소가 언급률 변동으로 오독된다.
const engCalls = cur.engine_calls || {};
const nCfg = Object.keys(engCalls).length || Object.keys(cur.engines).length;
const nOk = Object.keys(engCalls).length
  ? Object.values(engCalls).filter(n => n > 0).length
  : Object.keys(cur.engines).length;
const dead = Object.entries(cur.degraded || {}).filter(([,n]) => n === 0).map(([e]) => e);
document.getElementById('kpi').innerHTML = `
  <div class="item"><div class="num">${cur.total??'—'}%</div><div class="lbl">전체 언급률 ${deltaTag(cur.total, prev?prev.total:null)}</div></div>
  <div class="item"><div class="num">${DATA.ma4??'—'}%</div><div class="lbl">4주 이동평균${DATA.def_break_from?' <span class="down">현 정의 구간</span>':''}</div></div>
  <div class="item"><div class="num" ${nOk<nCfg?'style="color:var(--bad)"':''}>${nOk}/${nCfg}</div><div class="lbl">측정 엔진 ${nOk<nCfg?'<span class="down">커버리지 축소</span>':''}</div></div>
  <div class="item"><div class="num">${hist.length}</div><div class="lbl">누적 측정 회차</div></div>`;

if(DATA.def_break_from){
  const b = document.createElement('div');
  b.className = 'card';
  b.style.cssText = 'border-color:var(--bad);margin-bottom:16px';
  b.innerHTML = `<h2 style="color:var(--bad)">⚠ 별칭 정의 변경</h2>`
    + `<div style="font-size:13px;line-height:1.7;color:var(--ink)">`
    + `<b>${DATA.def_break_from}</b> 회차부터 브랜드로 세는 단어가 바뀌었다. `
    + `그 이전 회차는 <b>같은 지표가 아니므로</b> 추이선을 이어 읽으면 없던 하락·회복이 만들어진다.<br>`
    + `4주 이동평균은 현재 정의 구간에서만 계산했다.</div>`;
  document.getElementById('kpi').parentNode.insertBefore(b, document.getElementById('kpi').nextSibling);
}

if(dead.length){
  const b = document.createElement('div');
  b.className = 'card';
  b.style.cssText = 'border-color:var(--bad);margin-bottom:16px';
  b.innerHTML = `<h2 style="color:var(--bad)">⚠ 엔진 커버리지 경고</h2>`
    + `<div style="font-size:13px;line-height:1.7;color:var(--ink)">`
    + `<b>${dead.join(', ')}</b> 는 이번 회차(${cur.date}) 집계에서 통째로 빠졌다 `
    + `(기대 ${cur.engine_expected}콜 중 0콜 성공).<br>`
    + `엔진 구성이 다르므로 전회 대비 증감·추이선을 실적 변화로 읽으면 안 된다. `
    + `키·크레딧 복구 후 재측정이 필요하다.</div>`;
  document.getElementById('kpi').parentNode.insertBefore(b, document.getElementById('kpi').nextSibling);
}

// Radar (SVG)
(function(){
  const engines = Object.keys(cur.engines);
  const n = engines.length, W=380, H=300, cx=W/2, cy=H/2+8, R=105;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  for(const lv of [25,50,75,100]){
    const pts = engines.map((_,i)=>{
      const a = -Math.PI/2 + i*2*Math.PI/n, r = R*lv/100;
      return `${cx+r*Math.cos(a)},${cy+r*Math.sin(a)}`;
    }).join(' ');
    s += `<polygon points="${pts}" fill="none" stroke="#2a3550" stroke-width="1"/>`;
  }
  engines.forEach((e,i)=>{
    const a = -Math.PI/2 + i*2*Math.PI/n;
    s += `<line x1="${cx}" y1="${cy}" x2="${cx+R*Math.cos(a)}" y2="${cy+R*Math.sin(a)}" stroke="#2a3550"/>`;
    const lx = cx+(R+22)*Math.cos(a), ly = cy+(R+18)*Math.sin(a);
    s += `<text x="${lx}" y="${ly}" text-anchor="middle">${e} ${cur.engines[e]??'—'}%</text>`;
  });
  function poly(vals, color, fill){
    const pts = engines.map((e,i)=>{
      const v = (vals[e]??0)/100, a = -Math.PI/2 + i*2*Math.PI/n;
      return `${cx+R*v*Math.cos(a)},${cy+R*v*Math.sin(a)}`;
    }).join(' ');
    return `<polygon points="${pts}" fill="${fill}" stroke="${color}" stroke-width="2"/>`;
  }
  if(prev) s += poly(prev.engines, '#5b6b8c', 'rgba(91,107,140,.15)');
  s += poly(cur.engines, '#4da3ff', 'rgba(77,163,255,.25)');
  s += `</svg>`;
  if(prev) s += `<div class="note">파란색 = 이번 회차, 회색 = 전 회차</div>`;
  document.getElementById('radar').innerHTML = s;
})();

// Trend (SVG line)
(function(){
  const W=420,H=220,P=34;
  const xs = hist.map((_,i)=> P + i*(W-2*P)/Math.max(hist.length-1,1));
  const y = v => H-P - (v/100)*(H-2*P);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%">`;
  for(const g of [0,25,50,75,100]){
    s += `<line x1="${P}" y1="${y(g)}" x2="${W-P}" y2="${y(g)}" stroke="#2a3550"/>`;
    s += `<text x="${P-6}" y="${y(g)+4}" text-anchor="end">${g}</text>`;
  }
  const pts = hist.map((h,i)=>`${xs[i]},${y(h.total||0)}`).join(' ');
  s += `<polyline points="${pts}" fill="none" stroke="#4da3ff" stroke-width="2.5"/>`;
  hist.forEach((h,i)=>{
    s += `<circle cx="${xs[i]}" cy="${y(h.total||0)}" r="4" fill="#4da3ff"/>`;
    s += `<text x="${xs[i]}" y="${H-P+16}" text-anchor="middle">${h.date.slice(5)}</text>`;
    s += `<text x="${xs[i]}" y="${y(h.total||0)-10}" text-anchor="middle" style="fill:#e8edf6">${h.total}%</text>`;
  });
  s += `</svg>`;
  document.getElementById('trend').innerHTML = s;
})();

// 질문별 표
(function(){
  let rows = '<tr><th>질문</th><th style="width:90px">언급률</th><th style="width:120px"></th></tr>';
  for(const [qid, r] of Object.entries(cur.questions)){
    const t = DATA.question_texts[qid] || qid;
    rows += `<tr><td>${t}</td><td>${r}%</td><td><div class="bar"><i style="width:${r}%"></i></div></td></tr>`;
  }
  document.getElementById('qtable').innerHTML = rows;
})();

// 경쟁사 표 + URL
(function(){
  let rows = '<tr><th>경쟁사</th><th>언급</th></tr>';
  const cs = Object.entries(cur.competitors);
  if(!cs.length) rows += '<tr><td colspan="2" class="sub">언급 없음</td></tr>';
  for(const [c,n] of cs) rows += `<tr><td>${c}</td><td>${n}회 / ${cur.competitor_total}응답</td></tr>`;
  document.getElementById('ctable').innerHTML = rows;
  const domHtml = (cur.domains||[]).map(d=>`• ${d[0]} <span style="color:var(--acc)">${d[1]}회</span>`).join('<br>');
  const urlHtml = (cur.urls||[]).map(u=>`• ${u}`).join('<br>');
  document.getElementById('urls').innerHTML = domHtml || urlHtml || '(없음)';
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- doctor
def cmd_doctor(cfg, live=False):
    print("=== AEO 서비스 환경 점검 ===")
    print(f"질문 패널: {len(cfg['questions'])}개 질문 / 반복 {cfg['runs_per_question']}회 / "
          f"엔진 {', '.join(cfg['engines'])}")
    print(f"브랜드 별칭: {cfg['brand_aliases']}")
    if cfg.get("brand_aliases_weak"):
        confirm = cfg.get("brand_confirm") or cfg["brand_aliases"]
        print(f"약한 별칭(확증 필요): {cfg['brand_aliases_weak']} ← 확증어 {confirm}")
    n_calls = len(cfg["questions"]) * len(cfg["engines"]) * cfg["runs_per_question"]
    print(f"1회 실행 시 API 콜 수: {n_calls}")
    ok = True
    for engine in cfg["engines"]:
        env = key_env_for(engine)
        has = bool(os.environ.get(env))
        print(f"  - {engine:<11} {env:<19} {'✅ 있음' if has else '❌ 없음'}"
              f"  (모델: {model_for(cfg, engine)})")
        ok = ok and has
    if live:
        print("\n--- 라이브 테스트 (엔진별 1콜) ---")
        for engine in cfg["engines"]:
            try:
                res = ENGINE_FUNCS[engine]("대한민국의 수도는 어디인가요? 한 문장으로.", model_for(cfg, engine))
                print(f"  ✅ {engine}: \"{res['text'][:60].strip()}...\" (인용 {len(res['cited_urls'])}건)")
            except EngineError as e:
                print(f"  ❌ {engine}: {str(e)[:200]}")
                ok = False
    print("\n결론:", "정상 — `python3 aeo_service.py all` 실행 가능" if ok else "위 ❌ 항목을 해결하세요")
    # doctor는 "지금 측정을 돌릴 수 있나"만 판정한다(종료코드도 그 뜻). 운영 상태는
    # 참고로만 덧붙이고, 종료코드로 게이트하려면 `health`를 따로 부른다.
    print()
    cmd_health()
    return ok


# ---------------------------------------------------------------- health (운영 점검)
# Local history checks do not require any deployment account or credentials.

WEEKLY_OK_DAYS = 7       # 주 1회 패널: 이 안이면 정상
WEEKLY_WARN_DAYS = 13    # 1회차 결손 의심 (그 이상은 2회차 이상 중단)
ARCHIVE_AFTER_DAYS = 21  # 원장에 없고 이만큼 지났으면 '보관 패널'로 보고 결손 판정 제외


def panel_last_runs():
    """패널별 라이브 run 날짜 목록 (demo-runs는 제외 — 추세/판정 오염 방지)."""
    out = {}
    root = BASE / "data"
    if not root.is_dir():
        return out
    for pdir in sorted(root.iterdir()):
        rdir = pdir / "runs"
        if not pdir.is_dir() or not rdir.is_dir():
            continue
        dates = sorted(f.stem for f in rdir.glob("*.json"))
        if dates:
            out[pdir.name] = dates
    return out


def _days_since(date_str, today):
    try:
        return (today - datetime.date.fromisoformat(date_str)).days
    except ValueError:
        return None


def panel_engines(panel):
    """패널이 쓰기로 선언한 엔진 목록. 파일이 없으면 빈 리스트(그때는 run 실측으로 대체)."""
    path = QUESTIONS_FILE if panel == "default" else BASE / "panels" / f"{panel}.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("engines", list(DEFAULT_MODELS))
    except (json.JSONDecodeError, OSError):
        return []


def last_run_coverage(panel, date_str):
    """직전 회차의 엔진 커버리지 → (정상 엔진, 결손 엔진).

    엔진이 죽어도 나머지 엔진으로 회차는 '성공'하고, 줄어든 표본이 그대로
    언급률로 집계된다(6회차에서 잡은 '엔진 커버리지 은폐'). 죽은 엔진은
    서킷브레이커가 3회 실패 후 끊으므로 '기록 수가 적거나 전부 오류'로 나타난다.
    """
    path = BASE / "data" / panel / "runs" / f"{date_str}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list) or not records:
        return [], []
    total, failed = {}, {}
    for r in records:
        eng = r.get("engine")
        if not eng:
            continue
        total[eng] = total.get(eng, 0) + 1
        if r.get("error"):
            failed[eng] = failed.get(eng, 0) + 1
    if not total:
        return [], []
    expected = panel_engines(panel) or list(total)
    full = max(total.values())
    ok, dead = [], []
    for eng in expected:
        n, bad = total.get(eng, 0), failed.get(eng, 0)
        (dead if n == 0 or bad >= n or n < full else ok).append(eng)
    return ok, dead


def cmd_health():
    """Check local measurement cadence and engine coverage without network calls."""
    today = datetime.date.today()
    runs = panel_last_runs()
    print("=== 로컬 측정 상태 (비용 0) ===")
    print(f"오늘: {today.isoformat()}")
    if not runs:
        print("라이브 측정 기록 없음 — 초기 상태입니다. 무료 데모부터 실행할 수 있습니다.")
        return True
    problems = []
    for panel, dates in runs.items():
        last = dates[-1]
        lag = _days_since(last, today)
        if lag is None:
            problems.append(f"{panel}: 날짜를 확인하세요 ({last})")
            continue
        if lag > ARCHIVE_AFTER_DAYS:
            print(f"  - {panel}: {last} · 보관 패널")
            continue
        ok_engines, missing_engines = last_run_coverage(panel, last)
        print(f"  - {panel}: {last} · {lag}일 전 · 정상 엔진 {len(ok_engines)}개")
        if lag > WEEKLY_OK_DAYS:
            problems.append(f"{panel}: 마지막 측정 후 {lag}일 경과")
        if missing_engines:
            problems.append(f"{panel}: 직전 회차 엔진 결손 {', '.join(missing_engines)}")
    for problem in problems:
        print(f"  ⚠️ {problem}")
    print("결론:", "조치 필요" if problems else "측정 상태 정상")
    return not problems


# ---------------------------------------------------------------- 키워드 추가/모니터링
# 핸드북 6-1(질문 패널 생성) + 8-2(뾰족한 질문 선정)를 '비용 0 오프라인 가이드'로 옮긴 것.
# 새 키워드(질문)를 패널 JSON에 안전하게 추가하고, 추가 직후 가이드를 출력한다.

# 티어 자동분류용 한정어 그룹 — 그룹이 많이 결합될수록 '니치'(뾰족함).
# 주의: '은퇴/퇴직'은 연령이 아니라 '상황/맥락' 신호 — 대상/연령과 별개 그룹으로 둬야
#       "50대 은퇴 후 ~"가 2그룹(니치)으로 올바로 잡힌다.
_QUALIFIER_GROUPS = {
    "대상/연령": ["50대", "40대", "30대", "20대", "60대", "시니어", "직장인", "재직자",
              "주부", "학생", "초보", "비개발자", "예비창업", "사장", "대표"],
    "상황/맥락": ["은퇴", "퇴직", "노후", "부업", "투잡", "이직",
              "전직", "창업", "재취업", "인생 2막", "인생2막", "n잡", "사이드"],
    "지역": ["서울", "강남", "부산", "대구", "인천", "광주", "대전", "수원", "분당", "판교", "지방"],
    "포맷/제약": ["온라인", "오프라인", "무료", "단기", "1:1", "야간", "주말", "비대면", "왕초보"],
}
# 경쟁사 함정 — 포털·공공기관·위키류가 점유하기 쉬운 광범위 추천형 신호.
_BROAD_INTENT = ["추천", "어디가 좋", "어디서 배", "best", "베스트", "순위", "뭐가 있", "어디야", "어디예요"]
# 변형 제안 템플릿에서 잘라낼 꼬리 의도어 (핵심어 추출용).
# _core_phrase가 먼저 rstrip("?")를 하므로 '?'를 단 항목은 두지 않는다(매칭 불가 = 죽은 코드).
# 긴 표현이 먼저 잡히도록 길이/구체성 순으로 배치.
_INTENT_TAILS = ["추천해 줘", "추천해줘", "어디가 좋아", "어디서 배워", "뭐가 있어",
                 "알려줘", "어때", "추천"]


def resolve_panel_path(questions_arg):
    """--questions 인자를 절대경로로 해석 (load_config와 동일 규칙)."""
    qfile = Path(questions_arg) if questions_arg else QUESTIONS_FILE
    if not qfile.is_absolute():
        qfile = BASE / qfile
    return qfile


def panel_name_of(path):
    return "default" if path == QUESTIONS_FILE else path.stem


def load_raw_panel(path):
    """가공 없이(=setdefault 없이) 패널 JSON을 그대로 로드 — 원본 키/순서 보존."""
    if not path.is_file():
        sys.exit(f"[오류] 패널 파일이 없습니다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _inline(v):
    return json.dumps(v, ensure_ascii=False)


def dump_panel(data):
    """패널 JSON을 기존 손-포맷 스타일로 직렬화(질문은 한 줄 객체, 배열-스칼라는 인라인).

    표준 json.dumps(indent=2)는 모든 배열을 줄바꿈해 질문 한 줄 스타일이 깨지므로
    스키마(상위 dict / 스칼라·스칼라배열·dict·질문배열)에 맞춘 얕은 직렬화를 쓴다.
    """
    lines = ["{"]
    keys = list(data.keys())
    for ki, key in enumerate(keys):
        val = data[key]
        tail = "," if ki < len(keys) - 1 else ""
        kjson = json.dumps(key, ensure_ascii=False)
        if key == "questions" and isinstance(val, list):
            if not val:
                lines.append(f"  {kjson}: []{tail}")
                continue
            lines.append(f"  {kjson}: [")
            for qi, item in enumerate(val):
                qtail = "," if qi < len(val) - 1 else ""
                lines.append(f"    {_inline(item)}{qtail}")
            lines.append(f"  ]{tail}")
        elif isinstance(val, dict):
            if not val:
                lines.append(f"  {kjson}: {{}}{tail}")
                continue
            lines.append(f"  {kjson}: {{")
            subkeys = list(val.keys())
            for si, sk in enumerate(subkeys):
                stail = "," if si < len(subkeys) - 1 else ""
                lines.append(f"    {json.dumps(sk, ensure_ascii=False)}: {_inline(val[sk])}{stail}")
            lines.append(f"  }}{tail}")
        else:
            lines.append(f"  {kjson}: {_inline(val)}{tail}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_raw_panel(path, data):
    path.write_text(dump_panel(data), encoding="utf-8")


def next_question_id(questions):
    """기존 q\\d+ 중 최대 번호 +1 → qNN (없으면 q01). id는 삭제해도 재사용/재번호 안 함."""
    nums = []
    for q in questions:
        m = re.fullmatch(r"q(\d+)", str(q.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"q{(max(nums) + 1) if nums else 1:02d}"


def near_duplicates(text, questions, threshold=0.6):
    """정규화 후 SequenceMatcher 유사도 내림차순 (qid, qtext, ratio) — threshold 이상만."""
    norm = normalize(text)
    out = []
    for q in questions:
        ratio = difflib.SequenceMatcher(None, norm, normalize(q.get("text", ""))).ratio()
        if ratio >= threshold:
            out.append((q.get("id", "?"), q.get("text", ""), round(ratio, 2)))
    return sorted(out, key=lambda x: -x[2])


def matched_qualifier_groups(text):
    """질문에 등장한 한정어 그룹명 목록 (티어 분류 근거)."""
    norm = normalize(text)
    hits = []
    for group, terms in _QUALIFIER_GROUPS.items():
        if any(normalize(t) in norm for t in terms):
            hits.append(group)
    return hits


def classify_tier(text):
    """(tier, 근거문자열, 그룹목록) — 한정어 그룹 2개+ 결합이면 '니치', 아니면 '일반'."""
    groups = matched_qualifier_groups(text)
    if len(groups) >= 2:
        tier = "니치"
        why = f"한정어 {len(groups)}종 결합({'·'.join(groups)}) → 우리만 답이 될 수 있는 뾰족한 질문"
    elif len(groups) == 1:
        tier = "일반"
        why = f"한정어 1종({groups[0]})만 결합 → 조건을 1개 더 붙이면 니치로 전환 가능"
    else:
        tier = "일반"
        why = "한정어 없음 → 광범위 질문(경쟁 치열). 대상/상황/지역 조건을 붙이길 권장"
    return tier, why, groups


def competitor_trap_warning(text, groups):
    """광범위 추천형인데 한정어가 적으면 핸드북 8-2 함정 경고 반환 (아니면 None)."""
    norm = normalize(text)
    broad = any(normalize(b) in norm for b in _BROAD_INTENT)
    if broad and len(groups) < 2:
        return ("'추천/순위'류 광범위 질문은 포털·공공기관·위키가 상위 점유하기 쉬움(핸드북 8-2). "
                "조건 2~3개(대상·상황·지역)로 더 뾰족하게 좁히면 우리 점유 가능성↑")
    return None


def _core_phrase(text):
    """변형 제안용 핵심어 추출 — 꼬리 의도어 제거 후 트림 (완벽 NLP 아님, 템플릿용)."""
    core = text.strip().rstrip("?")
    for tail in _INTENT_TAILS:
        if core.endswith(tail):
            core = core[: -len(tail)].strip()
            break
    return core or text.strip().rstrip("?")


def suggest_variants(text, groups):
    """오프라인 템플릿 기반 연관 변형 질문 (API 호출 0). 핵심어를 의도/지역/뾰족 축으로 변주."""
    core = _core_phrase(text)
    out = [
        ("정보탐색형", f"{core} 비용 얼마야?"),
        ("후기검증형", f"{core} 후기/평판 어때?"),
    ]
    if "지역" not in groups:
        out.append(("지역한정형", f"[지역명] {core} 추천"))
    if len(groups) < 2:
        out.append(("뾰족화", f"[대상/상황] {core} 추천  ← 조건 1개 더 붙여 니치로"))
    return out


def estimate_calls(n_questions, engines, runs_per_question):
    return n_questions * max(len(engines), 1) * max(runs_per_question, 1)


def latest_question_rates(panel):
    """최근 run(라이브 우선, 없으면 데모)에서 질문별 언급률 best-effort 로드 (실패 시 {})."""
    global PANEL
    saved, PANEL = PANEL, panel
    try:
        for demo in (False, True):
            runs = load_runs(demo)
            if runs:
                return aggregate(runs[-1]).get("question_rates", {}), ("데모" if demo else "라이브")
        return {}, None
    finally:
        PANEL = saved


def _ask(prompt, default=""):
    """입력 헬퍼 — 비대화(EOF/파이프) 환경에서는 기본값으로 진행."""
    try:
        val = input(prompt).strip()
    except EOFError:
        print(f"(비대화 입력 → 기본값 '{default}')")
        return default
    return val or default


def _print_guidance(text, tier, why, groups, raw, added_calls_before, added_calls_after):
    """추가 직후 오프라인 가이드 블록 출력."""
    aliases = raw.get("brand_aliases", [])
    competitors = raw.get("competitors", {})
    norm = normalize(text)

    print("\n──────── 추가 가이드 (오프라인·비용 0) ────────")
    # 1) 경쟁사 함정
    trap = competitor_trap_warning(text, groups)
    if trap:
        print(f"⚠️  경쟁사 함정: {trap}")
    else:
        print("✅ 함정 신호 없음: 한정 조건이 충분 — 우리 점유를 노릴 만한 질문")
    # 2) 티어 근거
    print(f"🏷️  티어 '{tier}' — {why}")
    # 3) 비용 증분
    print(f"📊 주당 콜: {added_calls_before} → {added_calls_after} (+{added_calls_after - added_calls_before})  "
          f"= 질문수 × 엔진 {len(raw.get('engines', []))}개 × {raw.get('runs_per_question', 3)}회")
    # 4) 브랜드 별칭 커버리지 / 경쟁사 충돌
    print(f"🔎 브랜드 별칭 {len(aliases)}종 등록: {', '.join(aliases[:6])}{' …' if len(aliases) > 6 else ''}")
    comp_hit = [name for name, al in competitors.items()
                if any(normalize(t) in norm for t in ([name] + list(al or [])) if t)]
    if comp_hit:
        print(f"   └ ⚠️ 질문 안에 경쟁사 표기 포함({', '.join(comp_hit)}) — 판정이 경쟁사 쪽으로 기울 수 있음")
    # 5) 연관 변형 제안
    print("💡 연관 변형 제안(붙여 넣어 추가 측정 가능):")
    for label, v in suggest_variants(text, groups):
        print(f"   - [{label}] {v}")
    # 6) 보강 경로
    print("🔁 다음 측정에서 0% 나오면 → prompts/GEO_리라이트_프롬프트.md 로 글 보강 후 재측정")
    print("▶️  미리보기: python3 aeo_service.py all --demo   (또는 라이브: --demo 제거)")
    print("──────────────────────────────────────────────")


def cmd_keyword(args):
    """keyword add|list|remove — 대화형 위저드 기반 키워드 추가/조회/삭제 + 모니터링 연동."""
    global PANEL
    action = (args.action or "add").lower()
    if action not in ("add", "list", "remove"):
        sys.exit(f"[오류] keyword 하위 명령은 add|list|remove 중 하나 (받음: {action})")

    path = resolve_panel_path(args.questions)
    PANEL = panel_name_of(path)
    raw = load_raw_panel(path)
    raw.setdefault("questions", [])
    questions = raw["questions"]
    if not isinstance(questions, list) or any(not isinstance(q, dict) for q in questions):
        sys.exit(f"[오류] {path.name} 의 questions가 손상됐습니다(객체가 아닌 항목/배열 아님). 파일을 확인하세요.")
    engines = raw.get("engines", list(DEFAULT_MODELS))
    runs_pq = raw.get("runs_per_question", 3)
    print(f"[panel] {PANEL}  ({path.name}) · 현재 질문 {len(questions)}개")

    if action == "list":
        return _keyword_list(questions, engines, runs_pq)
    if action == "remove":
        return _keyword_remove(path, raw, questions, args)
    return _keyword_add(path, raw, questions, engines, runs_pq, args)


def _keyword_list(questions, engines, runs_pq):
    rates, mode = latest_question_rates(PANEL)
    print(f"\n=== 키워드(질문) 목록 — {len(questions)}개 · 주당 "
          f"{estimate_calls(len(questions), engines, runs_pq)}콜 ===")
    if rates:
        print(f"(최근 언급률 출처: {mode} run)")
    for q in questions:
        watch = "👁 " if q.get("watch") else "   "
        tier = q.get("tier", "?")
        rate = rates.get(q.get("id"))
        rate_s = f" · 최근 {rate}%" if rate is not None else ""
        print(f"  {watch}{q.get('id','?'):<4} [{tier:<2}] {q.get('text','')}{rate_s}")
    print("\n👁 = 워치(0%/급락 시 리포트·텔레그램 경보) · 추가: keyword add · 삭제: keyword remove --id qNN")


def _keyword_remove(path, raw, questions, args):
    qid = args.id
    if not qid:
        _keyword_list(questions, raw.get("engines", []), raw.get("runs_per_question", 3))
        qid = _ask("\n삭제할 키워드 id (예: q07, 빈 줄=취소)> ").strip()
    if not qid:
        print("취소했습니다.")
        return
    target = next((q for q in questions if q.get("id") == qid), None)
    if not target:
        sys.exit(f"[오류] id '{qid}' 질문을 찾을 수 없습니다.")
    print(f"삭제 대상: {qid} [{target.get('tier','?')}] {target.get('text','')}")
    if _ask("정말 삭제할까요? [y/N]> ", "n").lower() not in ("y", "yes"):
        print("취소했습니다.")
        return
    raw["questions"] = [q for q in questions if q.get("id") != qid]
    save_raw_panel(path, raw)
    print(f"[저장] {path.name} — '{qid}' 삭제 (질문 {len(raw['questions'])}개). "
          "id는 재번호하지 않습니다(추세 이력 보존).")


def _keyword_add(path, raw, questions, engines, runs_pq, args):
    print("\n=== 새 키워드(질문) 추가 위저드 ===")
    # 1) 질문 텍스트
    text = (args.text or "").strip() or _ask("질문 텍스트 (고객이 AI에게 물을 법한 한 문장, 빈 줄=취소)\n> ").strip()
    if not text:
        print("취소했습니다.")
        return

    # 2) 중복/유사 검사
    dups = near_duplicates(text, questions)
    if dups:
        print("\n[중복 검사] 유사한 기존 질문:")
        for qid, qtext, ratio in dups[:3]:
            print(f"   · {qid} (유사도 {ratio}) {qtext}")
        if dups[0][2] >= 0.85:
            if _ask("거의 동일한 질문이 있습니다. 그래도 추가할까요? [y/N]> ", "n").lower() not in ("y", "yes"):
                print("취소했습니다.")
                return
    else:
        print("[중복 검사] 통과 — 유사 질문 없음")

    # 3) 티어 (자동분류 → 확인/override)
    auto_tier, why, groups = classify_tier(text)
    if args.tier:
        tier = args.tier
        print(f"[티어] 지정값 사용: {tier}  (자동분류 참고: {auto_tier} — {why})")
    else:
        print(f"[티어] 자동분류: {auto_tier}  ({why})")
        sel = _ask("티어 [니치/일반] (Enter=자동분류 유지)> ", auto_tier).strip()
        tier = sel if sel in ("니치", "일반") else auto_tier

    # 4) 워치(모니터링 경보) 등록 여부
    if args.watch:
        watch = True
        print("[워치] --watch 지정 → 0%/급락 경보 등록")
    else:
        watch = _ask("0%/급락 시 경보 등록(watch)? [y/N]> ", "n").lower() in ("y", "yes")

    # 5) 확정 미리보기 → 저장
    qid = next_question_id(questions)
    before = estimate_calls(len(questions), engines, runs_pq)
    after = estimate_calls(len(questions) + 1, engines, runs_pq)
    print(f"\n추가하면: id {qid} · 티어 {tier}{' · 👁워치' if watch else ''} · 주당 콜 {before}→{after}")
    if _ask("진행할까요? [Y/n]> ", "y").lower() in ("n", "no"):
        print("취소했습니다.")
        return

    entry = {"id": qid, "text": text, "tier": tier}
    if watch:
        entry["watch"] = True
    raw["questions"].append(entry)
    save_raw_panel(path, raw)
    print(f"[저장] {path.name} — {qid} 추가 (질문 {len(raw['questions'])}개)")

    _print_guidance(text, tier, why, groups, raw, before, after)


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description="AEO 언급률 모니터링 서비스 (핸드북 9장 구현체)")
    parser.add_argument("command",
                        choices=["run", "report", "dashboard", "all", "doctor", "health", "keyword"])
    parser.add_argument("action", nargs="?", help="keyword 하위 명령: add | list | remove")
    parser.add_argument("--demo", action="store_true", help="API 호출 없이 데모 데이터로 실행")
    parser.add_argument("--date", help="run 날짜 강제 지정 (YYYY-MM-DD, 데모/백필용)")
    parser.add_argument("--live", action="store_true", help="doctor 시 엔진별 실호출 1회 수행")
    parser.add_argument("--questions", help="질문 패널 파일 경로 (기본: questions.json, 패널명=파일명)")
    parser.add_argument("--text", help="keyword add: 질문 텍스트 (생략 시 위저드가 물어봄)")
    parser.add_argument("--tier", choices=["니치", "일반"], help="keyword add: 티어 (생략 시 자동분류 후 확인)")
    parser.add_argument("--watch", action="store_true", help="keyword add: 0%%/급락 시 경보 등록")
    parser.add_argument("--id", help="keyword remove: 삭제 대상 질문 id (예: q07)")
    args = parser.parse_args()

    if not args.demo:
        load_env()

    if args.command == "keyword":
        cmd_keyword(args)
        return

    # health는 패널과 무관한 운영 점검이라 load_config 앞에서 처리한다.
    if args.command == "health":
        sys.exit(0 if cmd_health() else 1)

    cfg = load_config(args.questions)
    print(f"[panel] {PANEL}")

    if args.command == "run":
        cmd_run(cfg, demo=args.demo, date_str=args.date)
    elif args.command == "report":
        cmd_report(cfg, demo=args.demo)
    elif args.command == "dashboard":
        cmd_dashboard(cfg, demo=args.demo)
    elif args.command == "all":
        cmd_run(cfg, demo=args.demo, date_str=args.date)
        cmd_report(cfg, demo=args.demo)
        cmd_dashboard(cfg, demo=args.demo)
    elif args.command == "doctor":
        sys.exit(0 if cmd_doctor(cfg, live=args.live) else 1)


if __name__ == "__main__":
    main()
