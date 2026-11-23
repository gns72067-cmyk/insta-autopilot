---
name: insta-setup
description: insta-autopilot 셋팅 마법사 — 실행 환경 점검, 데이터 폴더 지정, 인스타 계정(카테고리·브랜드 색·로고·캐릭터·CTA·제외 키워드·추가 RSS) 등록과 샘플 카드 렌더. "인스타 셋팅", "계정 추가", "인스타 자동화 설정" 같은 요청에 사용.
---

# insta-setup — 셋팅 마법사

이용자 PC 에서 실행된다. 이용자 데이터(설정·브랜드 에셋·결과물·API 키)는 **이용자 PC 의 데이터 폴더에만** 저장하고 외부로 보내지 않는다.

## 실행 방법

- 플러그인 루트 `ROOT` = 환경변수 `CLAUDE_PLUGIN_ROOT` (없으면 이 SKILL.md 가 있는 폴더의 두 단계 위).
- 모든 명령은 `python "ROOT/scripts/ia.py" <command> ...` 로 실행한다 (Windows 에서 `python` 이 없으면 `py -3`).
  setup 스크립트가 만든 가상환경이 있으면 ia.py 가 알아서 그 파이썬으로 다시 실행한다.
- 명령 출력의 `✔ ⚠ ✘` 줄을 그대로 근거로 삼는다. 이용자 응답 없이 다음 단계로 넘어가지 않는다.
- 초안 파일(`draft.yaml`)은 데이터 폴더의 `accounts/<handle>/draft.yaml` 에 쓴다.

## 순서

### 1. 환경 점검
`ia.py doctor` → `✘` 필수 항목이 있으면 고치는 방법을 안내한다.
- 패키지·Chromium 이 없으면: Windows `powershell -ExecutionPolicy Bypass -File "ROOT/setup.ps1"`, macOS `bash "ROOT/setup.sh"` 실행을 제안하고, 이용자 동의 후 실행한다.
- ffmpeg 이 없으면 설치 명령(`winget install Gyan.FFmpeg` / `brew install ffmpeg`)을 안내한다(설치는 이용자가 직접).

### 2. 데이터 폴더
기본값 `~/Documents/InstaAutopilot` 을 제안하고 확인받는다. 메인 PC 에서 Claude 리모트로 밖에서도 결과물을 열어볼 수 있도록 실제 파일 폴더여야 한다.
- 확정되면 `ia.py setup_account init --home "<경로>"` (기본값이면 `--home` 생략).
- 채널: 기본 `stable`. 이용자가 새 규칙을 먼저 받아보고 싶다고 하면 `--channel beta`.
- **Wikimedia 연락처 = 이용자 본인 이메일.** Wikimedia Commons 는 자동 요청의 User-Agent 에 연락처를 요구한다. 이용자에게 이렇게 묻는다:
  "사진 검색(Wikimedia Commons)에 쓸 본인 이메일을 알려 주세요. 이 PC 의 config.yaml 에만 저장되고, Wikimedia 에 사진을 검색·다운로드할 때 요청 헤더로만 전달됩니다."
  - 이용자가 **직접 말한 본인 이메일만** `ia setup_account init --contact "<이메일>"` 로 저장한다. 다른 곳(계정 정보·git 설정 등)에서 찾아 채우지 않는다.
  - 이메일 형식이 아니면 명령이 `✘` 로 거부한다 → 다시 묻는다.
  - 원치 않으면 비워 두고(`--contact ""`), Wikimedia 검색은 건너뛰어 사진 후보가 줄어든다고 알린다.
- Unsplash·Pexels API 키는 선택. 이용자가 원하면 데이터 폴더 `config.yaml` 의 `api_keys` 에 **이용자가 직접** 붙여 넣게 안내한다(키를 대화에 적지 않도록).

### 3. 규칙 동기화 → 카테고리
`ia.py rules_sync` → 실패(`✘`)면 중단하고 사유를 알린다.
`ia.py setup_account catalog` 표를 보여 주고 **메인 1개 + 서브 최대 2개**를 고르게 한다.
- 목록에 없는 분야면: 이름을 직접 입력받고, 가장 가까운 카테고리를 메인으로 제안한다 → `custom_category: {name, based_on, trend_keywords: [...]}` 로 기록 (trend_keywords 는 이용자와 함께 5~10개).
- 서브 카테고리는 수집 소스·트렌드 키워드만 더해진다는 점을 설명한다.

### 4. 계정 정보
하나씩 묻는다:
- 인스타 핸들(`@` 없이 영문 소문자·숫자·`._`), 표시 이름(카드·CTA 에 보이는 브랜드 이름)
- **별칭(alias)**: 일괄 승인 때 부를 짧은 이름(예: `시사`, `연애`). 공백·쉼표·슬래시 없이 8자 이내, 계정끼리 겹치지 않게.
- 브랜드 색: `ia.py setup_account preset <메인id>` 의 팔레트 3안 중 선택 또는 직접(`#RRGGBB` 4색: accent·bg·cta·text).
- 칩 문구: 기본은 카테고리 이름. 6자 이내.
- 템플릿: 카테고리 기본값(A 마커형 / B 패널형 / C 타이포+캐릭터). 바꾸고 싶으면 지정.
- 로고·캐릭터 이미지(선택): 이용자가 파일 경로를 주면 `ia.py setup_account import-asset <handle> "<파일>" --kind logo|character` 로 계정 폴더에 복사하고, 출력된 상대 경로를 `brand.logo` / `brand.character` 에 쓴다. 브랜드 글꼴 파일도 `--kind font` 로 가능.

### 5. CTA 문구
preset 의 `default_cta`(pitch, pitch_em, card_desc, after)를 보여 주고 수정할 것만 받는다. `\n` 은 줄바꿈. `pitch_em` 은 pitch 안의 강조 부분이어야 한다.

### 6. 제외 키워드 · 추가 RSS · 톤
- `exclude_keywords`: 추천에서 뺄 단어(예: 특정 인물·브랜드).
- `extra_sources.rss`: `{name, url}` 목록. `extra_sources.google_news`: 검색어 목록.
- `tone`: 글쓰기 톤 한 줄(선택).
- 스타일 예시: `ia caption style-status <handle>` 이 0~2개면 과거 캡션 10~20개를 붙여 넣어 달라고 하거나(→ `ia caption style-add <handle> --file <파일>`, 예시 사이 `---`), 브라우저로 본인 프로필 최근 게시물 캡션을 읽어 채울지 묻는다(본인 계정만).

### 6-1. 업로드 방식 (`upload.method`)
먼저 **고지**한다(그대로 전달):
> 자동화된 방식의 게시는 인스타그램 약관·자동화 탐지 대상이 될 수 있습니다. 본인 판단으로 사용해 주세요. 언제든 방식 B(공식 API)나 C(수동)로 바꿀 수 있습니다.

| 방식 | 값 | 셋팅 | 설명 |
|---|---|---|---|
| A | `browser` (기본) | 없음 | 이 PC 브라우저에 로그인된 인스타그램 웹에서 Claude 가 올린다. 매번 로그인 계정을 확인한다 |
| B | `api` | 아래 B 안내 | 본인 Meta 개발자 앱 + Instagram API 토큰. 비즈니스/크리에이터 계정 필요. 예약 게시 가능 |
| C | `manual` | 없음 | 파일·복사용 캡션·체크리스트만 준비, 이용자가 직접 올림 |

**B 를 고르면 단계별로 안내한다** (Claude 가 대신 로그인·입력하지 않는다):
1. 인스타 계정을 비즈니스 또는 크리에이터 계정으로 전환(인스타 앱 설정 → 계정 유형).
2. developers.facebook.com 에서 앱 만들기 → 제품 "Instagram" → **Instagram 로그인을 사용하는 API 설정**.
3. 앱 역할에 본인 인스타 계정을 테스터로 추가하고 인스타 앱에서 초대 수락.
4. 권한 `instagram_business_basic`, `instagram_business_content_publish`(성과 기록을 쓰면 `instagram_business_manage_insights`)로 **액세스 토큰 생성**(장기 토큰).
5. 토큰은 **채팅에 붙여 넣지 말고**, 이용자가 자기 터미널에서 직접 실행하게 안내:
   `python "ROOT/scripts/ia.py" ig_api token-set <handle>` → 보이지 않게 입력 → 계정 일치 확인 후 OS 키체인에 저장.
   단기 토큰이면 `--short-lived` (앱 시크릿도 같은 방식으로 입력).
6. 확인: `ia ig_api me <handle>`, `ia ig_api limit <handle>`.
7. **임시 공개 URL**: Instagram 로그인 방식 API 는 PC 파일을 직접 받지 않으므로(2026-11-19 Meta 문서 확인) 영상을 잠시 공개 URL 에 올렸다가 게시 직후 지운다.
   config.yaml `temp_host` 를 이용자와 함께 채운다 — 시놀로지 공개 폴더(`type: folder`, `path`, `public_base_url`) 또는 S3·R2(`type: s3`, 키는 환경변수 이름만).
   규칙 서버(rules.*) 웹 루트는 쓰지 않는다. 확인: `ia tempurl test <아무 mp4>`.
8. 토큰은 만료 7일 전에 예약 작업이 자동 갱신하고, 실패하면 아침 브리핑에 알린다.

### 6-2. 사진 검색 키 (선택, 추천)
Unsplash·Pexels 는 무료 API 키가 있으면 사물·장소·상황 사진을 훨씬 많이 찾는다(키 없이도 Wikimedia·Flickr CC 는 검색됨). 이용자에게 원하면:
1. https://unsplash.com/developers → New Application → Access Key / https://www.pexels.com/api/ → API 키 받기 (무료).
2. 키는 **채팅에 붙여 넣지 말고** 이용자 본인 터미널에서: `python "ROOT/scripts/ia.py" setup_account set-key unsplash` (pexels 도 같은 방식).
3. 끝났다고 하면 `ia doctor` 로 확인.

### 6-3. 사진 사용 범위 — 언론사 기사 사진 사용 여부
이용자에게 묻는다: "언론사 기사 사진도 쓰시겠어요? 쓰시면 사진 사용 책임은 이용자 본인에게 있습니다."
- **사용 안 함** → `ia setup_account press off` (기본값. 저작권이 확인되는 출처만 씀)
- **사용** → 이용자에게 **본인 문장으로** 한 번 확인을 받는다('책임'이 들어가야 함. 예: "언론사 사진 사용 권한을 확인했고 책임은 나에게 있음").
  그 문장을 그대로 넘긴다: `ia setup_account press on --confirm "<이용자 문장 그대로>"` (Claude 가 문장을 지어내지 않는다).
  - 켜면 팩트 시트 출처 기사의 대표 사진이 사진 후보 맨 앞에 붙는다.
  - 켜고 끈 기록은 데이터 폴더 `press_photos_log.jsonl` 에 남는다. 상태 확인 `press status`.
- 사진 출처 표기 기본은 **끔**: 카드엔 쓰지 않고, 캡션엔 라이선스상 필요한 사진(Wikimedia·Flickr CC·공공누리)만 한 줄. 이용자가 카드에도 원하면 `ia setup_account photo-credit on`.
- 사진 배치 기본은 **꽉 채우기**. 이용자가 원하면 `ia setup_account photo-fit inset`(흐린 배경 + 사진 박스). 게시물마다 검토 화면에서도 바꿀 수 있다.

### 7. 샘플 → 확정
draft.yaml 예:
```yaml
handle: today_econ
display_name: 오늘경제
alias: 경제
main_category: economy
sub_categories: [finance]
custom_category: null
brand: {palette: {name: 딥그린 라임, accent: "#C6F432", bg: "#0F2A22", cta: "#FF6B35", text: "#FFFFFF"},
        chip: "", logo: assets/logo.png, character: "", font: ""}
template: ""
cta: {pitch: "숫자는 어렵게,\n설명은 쉽게", pitch_em: "설명은 쉽게"}
tone: ""
exclude_keywords: []
extra_sources: {rss: [], google_news: []}
upload: {method: browser}
active: true
```
1. `ia.py setup_account validate <draft.yaml>` → `✘` 가 있으면 고친다.
2. `ia.py setup_account sample <draft.yaml>` → `card.png`(샘플 카드), `cta_last.png`(CTA 마지막 프레임), `card_guide.png`(세이프존 가이드) 경로를 보여 주고(이미지를 열어 확인 가능하게) 확정 여부를 묻는다. 샘플 사진은 합성 이미지다.
3. 이용자가 확정하면 `ia.py setup_account save <draft.yaml>` → `profile.yaml` 저장. 수정 요청이면 4~6 으로 돌아간다.

### 8. 계정 추가
계정을 더 추가하려면 4번부터 다시 진행한다(마법사 재실행). `ia.py setup_account show` 로 등록된 계정을 보여 준다.
계정이 2개 이상이면 메인 작업에서 일괄 승인 모드(R1~R4)가 쓰인다는 점을 알린다. 잠시 빼려면 profile 의 `active: false`.

### 8-1. 검토 화면 (웹) — 어디서 볼지
완성본을 보고 고치고 업로드하는 화면이다(이 PC 에서 돌고, 외부 서버로 아무것도 보내지 않는다). 이용자에게 묻는다:
"검토 화면을 ① 이 PC 에서만 / ② 밖에서도 휴대폰으로 / ③ 같은 Wi-Fi 에서만 볼까요?"
- ① → `ia web start --where local`.
- ② → **Tailscale**(무료 개인 사설망)을 쓴다. 공개 인터넷에 열지 않고 본인 기기끼리만 연결된다. 설치는 이용자가 직접 한다:
  1. 이 PC: https://tailscale.com/download 에서 설치 → 로그인(구글·애플 등 본인 계정).
  2. 휴대폰: 앱스토어/플레이스토어에서 Tailscale 설치 → **같은 계정**으로 로그인 → 연결 켜기.
  3. 끝났다고 하면 `ia web start --where tailscale`. `✘` 면 메시지대로 안내(미설치·미연결).
- ③ → `ia web start --where lan` (집·사무실 같은 Wi-Fi 에서만. 카페 등 공용 Wi-Fi 에서는 쓰지 않도록 안내).
- 고른 범위는 config `web.where` 에 저장되어 다음부터 `ia web start` 만 하면 된다.
- 켠 뒤 `ia web open` 이 준 **주소와 접속 코드**를 보여 준다. 휴대폰에서 주소를 열고 코드를 한 번 넣으면 30일간 로그인된다.
  코드는 비밀번호처럼 다루라고 알린다(코드를 아는 사람은 업로드 버튼까지 쓸 수 있음). 바꾸려면 `ia web code --reset`.
- Windows 방화벽 허용 창이 뜨면 "개인 네트워크"만 허용하도록 안내한다.
- PC 를 켤 때마다 자동으로 켜려면 9번 아침 예약 작업이 `ia web start` 를 함께 실행한다.

### 8-2. 플러그인 자동 업데이트 켜기 (추천)
GitHub 에서 설치한 플러그인은 자동 업데이트가 **기본으로 꺼져 있다.** 이용자에게 안내:
입력창에 `/plugin` → **Marketplaces** 탭 → **insta-autopilot** → **Enable auto-update**.
켜 두면 새 버전이 나왔을 때 "Plugin updated … Run /reload-plugins" 알림이 뜨고, `/reload-plugins` 또는 새 세션에서 적용된다.
(규칙은 이와 별개로 작업마다 자동 갱신된다.)

### 9. 매일 아침 추천 예약 (마지막 단계)
이 PC(메인 PC)에서 매일 아침 자동으로 추천 브리핑을 만들도록 **Claude 데스크톱 앱 예약 작업** 등록을 안내한다.
- 시각: 기본 07:30. 등록된 계정들의 카테고리 `posting_time_hint` 중 가장 이른 시각의 1시간 전이 더 이르면 그 시각을 제안한다(예: 07:00 게시 → 06:00).
- 이용자가 동의하면, 예약 작업 도구(scheduled-tasks)가 있으면 직접 만들고, 없으면 데스크톱 앱의 예약 작업 화면에서 만드는 방법을 안내한다:
  - 이름: `insta-autopilot 아침 브리핑`
  - 작업 폴더: 데이터 폴더
  - 프롬프트: `insta-autopilot 아침 브리핑 — 규칙 동기화 후 전 계정 추천을 만들고 R1 브리핑에서 멈춰 줘.` (검토 화면도 이때 켜진다)
- 결과 파일은 이 PC 의 출력 폴더에 남고, 이용자는 폰(리모트)에서 그 세션으로 이어서 답하면 된다. 메인 PC 가 켜져 있고 앱이 실행 중이어야 한다.
- 방식 B 를 쓰고 예약 게시(`--at`)를 쓰면, 같은 방식으로 15분마다 `insta-autopilot 예약 게시 확인 — ia upload due 실행` 작업도 제안한다.

## 하지 말 것
- 규칙 내용(카테고리·팔레트·CTA 기본값)을 이 문서나 코드에서 지어내지 않는다 — 항상 `catalog`/`preset` 출력에서 가져온다.
- 이용자가 직접 주지 않은 이메일·키를 넣지 않는다. API 키를 대화창에 출력하지 않는다.
- 확인 없이 `save` 하지 않는다.
- 토큰·비밀번호·앱 시크릿을 채팅으로 받지 않는다. 받았으면 쓰지 말고 즉시 폐기·재발급을 권한다.
- 예약 작업은 이용자 동의 없이 만들지 않는다.
