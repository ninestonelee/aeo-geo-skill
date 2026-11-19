# AEO·GEO Skill — 수업용 AI 브랜드 언급 측정

고객이 AI에 물을 질문을 만들고, 답변에서 내 브랜드와 경쟁사가 얼마나 언급되는지 측정한 뒤 콘텐츠를 개선하는 **Codex·Claude Code용 한국어 스킬**입니다.

**처음에는 API 키가 필요 없는 무료 데모로 시작하세요.** 데모는 키 파일을 읽거나 네트워크에 연결하지 않으며 텔레그램 알림도 보내지 않습니다. 실제 측정은 별도 API 키와 사용 요금이 필요합니다.

| 바로가기 | 내용 |
|---|---|
| [수업용 ZIP 다운로드](https://github.com/ninestonelee/aeo-geo-skill/releases/latest/download/aeo-geo-skill.zip) | 스킬 폴더만 담은 설치 파일 |
| [버전별 배포·체크섬](https://github.com/ninestonelee/aeo-geo-skill/releases) | 수업 버전 고정·파일 무결성 확인 |
| [30분 실습 안내](CLASSROOM.md) | 설치 → 질문 설계 → 데모 → 결과 해석 → 글 개선 |
| [상세 사용 설명서](aeo-geo/사용설명서.md) | 터미널 명령·키 설정·다중 패널·오류 대응 |
| [변경 이력](CHANGELOG.md) | 수업 배포본의 변경 내용 |

## 1. 가장 쉬운 설치: AI에게 요청

Codex 또는 Claude Code에 다음 내용을 붙여 넣으세요.

```text
https://github.com/ninestonelee/aeo-geo-skill 의 aeo-geo 폴더를
현재 사용자 계정의 글로벌 스킬로 설치해줘.
수업 버전은 v1.0.0을 사용해줘.
기존 동명 스킬이 있으면 덮어쓰지 말고 위치를 알려줘.
설치만 하고 유료 API 호출이나 알림 전송은 하지 마.
```

글로벌 설치는 현재 사용자 계정의 여러 프로젝트에서 사용한다는 뜻입니다. 다른 컴퓨터에는 별도 설치가 필요합니다. 설치 뒤 다음 메시지에서 사용하고, 인식되지 않으면 새 채팅을 열어 주세요.

## 2. 직접 설치

Python 3.11 이상이 필요합니다. 필수 pip 패키지는 없습니다. Windows에서 `python3`가 없다면 `py -3` 또는 설치된 `python`을 사용하세요.

### Git 사용

```bash
git clone --branch v1.0.0 https://github.com/ninestonelee/aeo-geo-skill.git
cd aeo-geo-skill
python3 scripts/install.py --app codex
```

Claude Code용 설치:

```bash
python3 scripts/install.py --app claude
```

설치기는 기존 폴더를 덮어쓰지 않습니다. Codex는 기본 `~/.codex/skills/aeo-geo`, Claude Code는 `~/.claude/skills/aeo-geo`에 설치합니다. `CODEX_HOME`이 설정되어 있으면 Codex 설치 위치에 반영합니다. `--dest /원하는/skills/aeo-geo`로 전체 대상 경로를 지정할 수도 있습니다.

### Git 없이 ZIP 사용

1. 위의 **수업용 ZIP 다운로드**를 클릭합니다.
2. 압축을 풀면 `aeo-geo` 폴더가 나옵니다.
3. 폴더 전체를 아래 경로에 넣습니다. 기존 폴더가 있으면 덮어쓰지 마세요.

| 도구 | macOS·Linux | Windows 기본 경로 |
|---|---|---|
| Codex | `~/.codex/skills/aeo-geo` | `%USERPROFILE%\.codex\skills\aeo-geo` |
| Claude Code | `~/.claude/skills/aeo-geo` | `%USERPROFILE%\.claude\skills\aeo-geo` |

`aeo-geo/aeo-geo/SKILL.md`처럼 이중 폴더가 되지 않고, 설치한 `aeo-geo` 폴더 바로 아래에 `SKILL.md`가 있어야 합니다. GitHub의 **Code → Download ZIP**은 저장소 전체 압축 파일이며, 릴리스의 수업용 ZIP과 구조가 다릅니다.

## 3. 수업 첫 요청

Codex에서는 다음처럼 요청합니다. Claude Code에서도 같은 내용을 자연어로 요청하면 됩니다.

```text
$aeo-geo 이 프로젝트에 AEO 측정 환경을 만들어줘.
브랜드: 예시아카데미
서비스: 비개발자를 위한 AI 마케팅 실습 교육
고객: 직장인과 소상공인
지역: 서울과 온라인
경쟁사: 수업에서 정한 비교 브랜드

고객 질문 후보 20개를 만들고 니치 질문 5개를 골라줘.
질문에 우리 브랜드명을 직접 넣지 마.
무료 데모만 실행하고 리포트와 대시보드를 설명해줘.
실제 API 호출, 텔레그램 전송, 예약 실행은 하지 마.
```

스킬은 프로젝트 안에 `aeo-monitor/`를 만들고 엔진·질문 패널을 복사합니다. 글로벌 설치 폴더에서 측정하지 않습니다.

## 4. 무엇을 배우나

- **질문 패널:** 고객이 실제로 물을 질문과 브랜드·경쟁사 별칭 정의.
- **AEO 측정:** 질문마다 엔진별 기본 3회 질의, 성공 응답 중 브랜드 언급 비율 집계.
- **리포트:** 질문별·엔진별 언급률, 경쟁사, 인용 URL, 데이터 결손 확인.
- **GEO 개선:** 질문에 대한 직접 답변과 검증 가능한 근거·출처·FAQ 보강.
- **추세 해석:** API 답변과 소비자 앱의 차이, 미측정과 0%, 반복 표본의 한계 이해.

질문 5개 × 엔진 4개 × 3회 = 기본 60회 호출입니다. 실측 비용은 모델·검색·토큰·계정 조건에 따라 다릅니다. 제공 모델 기본값의 현재 계정별 사용 가능 여부는 실측 전에 확인하세요.

## 5. 자료 구성

```text
aeo-geo/                    # 설치할 스킬 폴더
  SKILL.md                  # AI 에이전트용 실행 지침
  사용설명서.md              # 수강생용 상세 안내
  scripts/aeo_service.py     # 측정 엔진
  templates/questions.json # 질문 패널 예제
  templates/.env.example   # 값이 비어 있는 키 설정 양식
  references/              # 핸드북·질문 생성·GEO 프롬프트
CLASSROOM.md                # 30분 수업 실습
scripts/install.py          # 덮어쓰기 없는 설치기
scripts/build_release.py    # 재현 가능한 ZIP·체크섬 생성
tests/                      # 외부 API 없는 기능 검증
```

## 6. 결과 해석의 한계

API 응답은 소비자 ChatGPT·Gemini·Claude 앱 화면과 다릅니다. 0%는 이번 성공 응답 표본에서 브랜드가 언급되지 않았다는 뜻이며, 미측정이나 오류와 다릅니다. 최근 4회 이동평균은 주 1회 실행할 때 4주 평균입니다. GEO 개선이 노출이나 매출 증가를 보장하지 않습니다.

참고 핸드북의 과거 가격·정책·효과 수치는 현재 보장값이 아닙니다. 업무에 적용할 때 공식 자료를 다시 확인하세요. API 키, 실제 고객 측정 데이터, 개인 설치 기록은 배포본에 포함하지 않습니다.

## 개발·검증

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_release.py --version v1.0.0
```

테스트는 실제 API를 호출하지 않습니다. `dist/aeo-geo-skill.zip`과 `dist/SHA256SUMS`가 생성됩니다. GitHub Actions에서도 무료 검증을 실행합니다.
