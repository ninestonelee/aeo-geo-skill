---
name: aeo-geo
description: AI 답변의 브랜드·경쟁사 언급률을 측정하고 질문 패널·리포트·GEO 리라이트·사이트 기술 점검을 돕는 한국어 스킬. AEO, GEO, AI 검색 노출, AI 답변에 우리 브랜드 나와, 언급률 측정, 질문 패널, GEO 리라이트 요청에 사용한다. 수업에서는 키와 네트워크가 필요 없는 무료 데모부터 시작한다.
---

# AEO·GEO 측정·개선 — 수업 배포본 v1.0.0

측정 엔진은 `scripts/aeo_service.py`이며 필수 외부 Python 패키지는 없다. 사용자 안내는 `사용설명서.md`, 원리 참고는 `references/AEO_GEO_실전핸드북.md`를 읽는다. 참고자료의 과거 수치·모델·정책을 현재 보장값으로 설명하지 않는다.

아래 `$SKILL`은 이 파일이 있는 폴더의 절대경로다.

## 1. 프로젝트별 환경

프로젝트 안에 `aeo-monitor/`를 만들고 엔진·템플릿·프롬프트를 복사한다. 엔진은 자기 파일 옆에 결과를 저장하므로 글로벌 스킬 폴더에서 측정하지 않는다.

```bash
mkdir -p aeo-monitor/prompts
cp "$SKILL/scripts/aeo_service.py" aeo-monitor/
if [ ! -e aeo-monitor/questions.json ]; then
  cp "$SKILL/templates/questions.json" aeo-monitor/
fi
cp "$SKILL"/references/prompts/*.md aeo-monitor/prompts/
```

기존 질문 패널은 덮어쓰지 않는다. 엔진을 교체하기 전 프로젝트에서 수정한 버전이 있는지 확인한다.

## 2. 질문 패널

브랜드·서비스·고객·지역·경쟁사를 파악하고 `questions.json`을 설정한다.

- `brand_aliases`: 내 브랜드가 확실한 한글·영문·줄임말.
- `competitors`: 경쟁사별 별칭 배열.
- `questions`: 질문 후보 20개에서 니치 질문 5~10개 선정. 비보조 인지도 측정 질문에는 브랜드명을 직접 넣지 않는다.
- 일반어와 겹치는 이름은 `brand_aliases_weak`에, 확증어는 `brand_confirm`, 오탐 구절은 `brand_exclude`에 둔다.
- `engines`: 사용할 엔진만 지정. `runs_per_question`은 기본 3회.

초기 패널 작성 후 개별 질문 추가는 중복 검사·티어 안내가 있는 위저드를 사용한다.

```bash
python3 aeo_service.py keyword add --text "고객 질문" --tier 니치 --watch
python3 aeo_service.py keyword list
```

## 3. 데모와 실측

수업·체험 요청은 데모부터 실행한다.

```bash
python3 aeo_service.py all --demo
```

데모는 합성 데이터다. 키 파일을 로드하지 않고 네트워크나 텔레그램을 사용하지 않는다. 실제 성과처럼 보고하지 않는다.

실측을 요청받았을 때만 점검·측정한다.

```bash
python3 aeo_service.py doctor
python3 aeo_service.py doctor --live
TELEGRAM_BOT_TOKEN="" TELEGRAM_CHAT_ID="" python3 aeo_service.py all
```

- `doctor`는 API 호출 없이 설정을 점검한다. `doctor --live`는 엔진별 실호출로 비용이 발생할 수 있다.
- 키는 환경변수 → 프로젝트 엔진 옆 `.env` 순서로 로드한다. 개인 중앙 키 파일은 읽지 않는다.
- 지원 키: `OPENAI_API_KEY`, `GEMINI_API_KEY_PAID` 또는 `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `ANTHROPIC_API_KEY`.
- 키 값은 출력하지 않는다. 키가 없는 엔진은 실측에서 제외된다.
- 실측 전에 질문 수 × 사용 가능한 엔진 수 × 반복 수와 별도 테스트 호출 수를 알려준다. 비용을 고정 금액으로 보장하지 않는다.
- 실측 리포트는 텔레그램 토큰·채팅 ID가 있으면 전송될 수 있다. 알림을 명시적으로 요청하지 않았다면 위처럼 두 변수를 빈 값으로 지정한다.

## 4. 결과와 해석

- 실측: `data/{패널}/runs/{날짜}.json`, `reports/{패널}/{날짜}_AEO리포트.md`, `reports/{패널}/dashboard.html`.
- 데모: `data/{패널}/demo-runs/`, `reports/{패널}/demo/`로 분리된다.
- 다른 패널은 `--questions panels/이름.json`을 붙인다. 파일명이 패널명이다.
- 같은 날짜·패널·모드는 덮어써질 수 있다. 이미 전달한 측정본은 재생성하지 않는다.
- JSON 답변 기록은 최대 200자 발췌이며 전체 답변 보관본이 아니다.

API 응답은 소비자 앱 화면과 다르다. 미측정·오류와 0%를 구분한다. 0%는 이번 성공 응답 표본에서 미언급이라는 뜻이다. 최근 4회 이동평균은 주 1회 실행할 때 4주 평균이다. 질문·별칭·엔진·모델이 바뀌면 비교 기준도 달라진다.

낮은 언급률 질문은 `references/prompts/GEO_리라이트_프롬프트.md`로 글을 보강한다. 수치·출처·인용문을 검증하고 없으면 필요한 자료로 표시한다. 콘텐츠 수정이 노출·매출 증가를 보장하지 않는다.

사이트 기술 점검을 요청받으면 실제 사이트와 최신 공식 자료를 확인한다. 예약 실행·외부 게시·텔레그램 전송은 해당 작업을 요청받았을 때만 진행한다.
