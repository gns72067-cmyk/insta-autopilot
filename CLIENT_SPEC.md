# CLIENT_SPEC — insta-autopilot 규칙 번들 클라이언트 명세

플러그인(클라이언트)이 규칙 번들을 **받고, 검증하고, 합성하고, 렌더하는** 데 필요한 내용을 모두 담는다.
이 문서와 다르게 동작하는 서버 파일이 있으면 그것은 오류이며, 클라이언트는 **받아들이지 않는다**.

- 문서 버전: schema 2 (2026-11-19)
- 참조 구현: [tools/client_verify.py](tools/client_verify.py) (Python, 저장소 코드와 독립), 8장의 JavaScript 예시
- 용어: "반드시"(MUST) / "해야 한다"(SHOULD) / "해도 된다"(MAY)

---

## 1. 서버 URL 구조

기준 주소(BASE): **`https://rules.sonicent.co.kr`** (HTTPS 만. 끝에 `/` 없음)

| 경로 | 내용 | 바뀌는가 | 캐시 |
|---|---|---|---|
| `/stable/latest.json` | stable 채널 포인터 (서명 봉투) | 배포·promote·rollback 때 교체 | 하지 않음 |
| `/beta/latest.json` | beta 채널 포인터 (서명 봉투) | 배포·rollback 때 교체 | 하지 않음 |
| `/bundles/<ver>.tar.gz` | 규칙 번들 | **불변** | 영구 가능 |
| `/bundles/<ver>.manifest.json` | 번들 파일 목록·해시 (서명 봉투) | **불변** | 영구 가능 |
| `/pubkey.pem` | 공개키 사본 (참고용) | 키 교체 시 | — |

- `<ver>` 는 `MAJOR.MINOR.PATCH` (예: `1.0.2`). 정규식 `^\d+\.\d+\.\d+$`.
- 위 다섯 종류 외의 파일은 서버에 없다(배포 때 자동 삭제). `.sig` 파일은 **존재하지 않는다**.
- 디렉터리 목록(`/`, `/bundles/`)은 403 이다. 클라이언트는 목록을 조회하지 않는다.
- **`pubkey.pem` 을 신뢰 근거로 쓰지 않는다.** 클라이언트는 2장의 내장 공개키만 쓴다.

### 1.1 요청 방법

- `latest.json` 은 캐시를 피해 요청한다: 쿼리 `?t=<유닉스 밀리초>` + 헤더 `Cache-Control: no-cache`.
- 리디렉션은 같은 BASE 호스트 안에서만 따라간다. 다른 호스트로 가면 실패로 처리한다.
- 타임아웃 30초. 200 이외 응답은 실패.
- 확인 시점: 작업(세션) 시작 때마다 1회. 진행 중인 작업은 시작 때 고정한 번들 버전을 끝까지 쓴다.

---

## 2. 공개키

| 항목 | 값 |
|---|---|
| 알고리즘 | **Ed25519** (RFC 8032, 순수 Ed25519 — 사전 해시 없음, context 없음) |
| 내장 형식 | raw 공개키 32바이트의 **표준 base64**(패딩 포함, RFC 4648 §4) |
| 값 | **`/OPxSlRxnd/fvMH3VovdNQWEUaSN+PxiLRo2hsctfUo=`** |
| 같은 키의 PEM (SPKI) | `-----BEGIN PUBLIC KEY-----` / `MCowBQYDK2VwAyEA/OPxSlRxnd/fvMH3VovdNQWEUaSN+PxiLRo2hsctfUo=` / `-----END PUBLIC KEY-----` |
| SPKI DER 접두 | 공개키 32바이트 앞에 hex `302a300506032b6570032100` 를 붙이면 SPKI DER 가 된다 |

- 공개키는 플러그인 코드에 **상수로 내장**한다. 서버·설정 파일에서 읽어 교체하지 않는다.
- 키가 교체되면 플러그인 새 버전을 배포해야 한다(서버 측은 키 교체 시 `min_client_version` 을 올린다).

---

## 3. 서명 봉투 형식 (latest.json · manifest.json 공통)

파일 하나에 내용과 서명이 함께 있다. 파일 교체가 원자적이므로 내용과 서명이 어긋난 상태는 생기지 않는다.

```json
{
  "payload": { … 서명 대상 객체 … },
  "sig": "<base64 ed25519 서명 64바이트>"
}
```

### 3.1 봉투 규칙

1. 파일은 UTF-8 JSON. BOM 없음. 공백·들여쓰기·줄바꿈은 의미 없다(서버는 들여쓰기 2칸으로 쓴다).
2. 최상위는 객체이고 키는 정확히 `payload`, `sig` 두 개다. 다른 키가 있거나 빠지면 **거부**.
3. `payload` 는 객체. `sig` 는 문자열.
4. `sig` 를 **표준 base64(패딩 포함)** 로 엄격하게 디코드한다. 결과가 정확히 64바이트가 아니면 거부.
5. 서명 입력 = `canonical(payload)` 의 UTF-8 바이트 (3.2).
6. `Ed25519.verify(공개키, 서명, 입력)` 이 실패하면 거부.

### 3.2 정규화 `canonical(payload)` — RFC 8785 (JCS) 호환

서버의 payload 값은 **문자열, 정수, true/false, null, 배열, 객체**뿐이다. 실수(소수점·지수)는 쓰지 않으며, 정수는 |n| ≤ 2^53−1, 객체 키는 모두 ASCII 다. 이 범위에서 정규화는 다음과 같다.

- 객체: 키를 **코드포인트 오름차순**으로 정렬, `{"키":값,"키":값}` — 쉼표·콜론 주변 공백 없음.
- 배열: 순서 유지, `[값,값]`.
- 문자열: `"` 로 감싼다. 이스케이프는 `\"` `\\` `\b` `\f` `\n` `\r` `\t`, 그 밖의 U+0000–U+001F 는 `\u00xx`(**소문자** hex). 그 외 문자(한글 포함, `/` 포함)는 이스케이프하지 않고 UTF-8 그대로.
- 정수: 10진수, 앞자리 0·`+` 없음. `true` `false` `null` 은 그대로.
- 파일 마지막 줄바꿈 등 payload 바깥 내용은 서명과 무관하다.

언어별로 이렇게 얻는다:

| 언어 | 정규화 |
|---|---|
| JavaScript | 키를 재귀 정렬한 뒤 `JSON.stringify` (8장 코드). 또는 `canonicalize` npm 패키지(RFC 8785) |
| Python | `json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 를 UTF-8 인코딩 |
| 기타 | RFC 8785 구현 라이브러리 |

### 3.3 테스트 벡터 (실제 서명 키로 서명함)

```json
{"payload":{"a":{"b":"시작","z":"끝"},"list":[1,true,null],"schema":2,"text":"규칙 \"따옴표\"\n줄바꿈"},"sig":"3KtbKNc9hrKW9SxpaP98VZ97MI730LlpeXYARjgK65gOGCLdze8TX+rBDlqbNkaImkUf6z1luPoAFhWgLOrwDg=="}
```

- 정규화 결과(서명 입력)는 정확히 다음 문자열의 UTF-8 바이트다:
  `{"a":{"b":"시작","z":"끝"},"list":[1,true,null],"schema":2,"text":"규칙 \"따옴표\"\n줄바꿈"}`
- 위 봉투는 2장의 공개키로 **검증에 성공**해야 한다. `sig` 의 아무 한 글자나 payload 의 아무 값을 바꾸면 **실패**해야 한다.

---

## 4. 파일별 필드

### 4.1 `/<channel>/latest.json` 의 payload

| 필드 | 타입 | 설명 |
|---|---|---|
| `schema` | 정수 | `2`. 다른 값이면 거부(새 형식 → 플러그인 업데이트 필요) |
| `channel` | 문자열 | `"stable"` 또는 `"beta"`. **요청한 채널과 같아야 함** |
| `version` | 문자열 | semver `X.Y.Z` |
| `bundle` | 문자열 | BASE 기준 상대 경로. 항상 `bundles/<version>.tar.gz` |
| `sha256` | 문자열 | 번들 파일 SHA-256, 소문자 hex 64자 |
| `size` | 정수 | 번들 파일 바이트 수 |
| `manifest` | 문자열 | 항상 `bundles/<version>.manifest.json` |
| `manifest_sha256` | 문자열 | **manifest 파일 바이트 전체**(봉투 포함, 받은 그대로)의 SHA-256 소문자 hex |
| `changelog` | 문자열 배열 | 이 버전 변경 요약. 이용자에게 그대로 보여 준다 |
| `min_client_version` | 문자열 | 이 번들을 쓸 수 있는 최소 플러그인 버전 (semver) |
| `published_at` | 문자열 | 포인터 서명 시각. ISO 8601, 초 단위, 오프셋 포함 (예: `2026-11-19T12:21:29+09:00`) |

- `bundle`·`manifest` 가 위 형태가 아니거나 `..`·`/` 로 시작하면 거부한다(경로 조작 방지).
- 롤백도 새 `published_at` 으로 다시 서명된다. 그래서 버전은 낮아질 수 있지만 `published_at` 은 낮아지지 않는다.

### 4.2 `/bundles/<ver>.manifest.json` 의 payload

| 필드 | 타입 | 설명 |
|---|---|---|
| `schema` | 정수 | `2` |
| `version` | 문자열 | latest.json 의 `version` 과 같아야 함 |
| `created_at` | 문자열 | 빌드 시각 (ISO 8601) |
| `bundle` | 객체 | `{path, sha256, size}` — `path` = `bundles/<ver>.tar.gz`, 해시·크기는 latest.json 과 같아야 함 |
| `files` | 배열 | 번들 안 **모든** 파일: `{path, sha256, size}` |
| `changelog` | 문자열 배열 | 이 버전 변경 요약 |
| `min_client_version` | 문자열 | latest.json 과 같은 값 |

### 4.3 번들 `<ver>.tar.gz`

- gzip 으로 압축한 tar(PAX). 일반 파일만 있다(디렉터리·링크 항목 없음). 경로 구분자 `/`, 상대 경로.
- 루트 구조:

```
bundle.json                          {"changelog":[...], "version":"X.Y.Z"}
base/workflow.md  brief.md  fact.md  hook.md  image.md  caption.md  upload.md
base/checks.yaml  base/banned_phrases.txt
categories/_catalog.yaml
categories/<id>/preset.yaml          (+ 오버라이드: brief.md, fact.md, hook.md, image.md, caption.md, upload.md, banned_phrases.txt 중 일부)
templates/reel_9x16.html  feed_4x5.html  cta_follow.html  base.css  slots.js  template_meta.yaml
```

- 텍스트 파일은 UTF-8. 줄끝은 LF 또는 CRLF 가 섞여 있을 수 있다(파서는 둘 다 허용).
- 폰트 파일은 들어 있지 않다(클라이언트가 보유).

---

## 5. 검증 순서 (반드시 이 순서, 하나라도 실패하면 중단)

```
입력: channel ("stable" 기본, 이용자가 beta 를 켰으면 "beta")
상태: cache_dir(현재 쓰는 번들), last = {version, published_at} (마지막으로 받아들인 포인터)
```

1. **포인터 받기**: GET `BASE/<channel>/latest.json`. 3장 규칙으로 봉투 검증.
   검증 실패 시 5초 후 1회 재시도. 그래도 실패하면 **현재 캐시를 그대로 쓰고** 경고만 기록한다.
2. **포인터 내용 확인**
   - `schema == 2`, `channel == 요청 채널`
   - `last.published_at` 이 있으면 `published_at >= last.published_at` (시각으로 비교, 문자열 비교 금지). 과거면 재전송 공격으로 보고 거부.
   - `version == last.version` 이고 `published_at == last.published_at` 이면 **변경 없음** → 여기서 끝.
3. **클라이언트 버전**: `min_client_version > 플러그인 버전`(semver 비교)이면 번들을 받지 않고 "플러그인 업데이트 필요"를 안내. 현재 캐시는 계속 쓴다.
4. **manifest**: GET `BASE/<manifest>`. 받은 바이트 그대로의 SHA-256 이 `manifest_sha256` 과 같은지 → 봉투 검증 → `payload.version == version`, `payload.bundle.sha256 == sha256`, `payload.bundle.size == size`.
5. **번들**: GET `BASE/<bundle>`. 바이트 수 == `size`, SHA-256 == `sha256`.
6. **풀기**: 임시 폴더에 푼다. 각 항목에 대해
   - 일반 파일이 아니면 거부, 경로가 절대경로이거나 `..` 를 포함하거나 `\` 를 포함하면 거부
   - `files` 에 없는 경로, 같은 경로 중복이면 거부
   - 내용의 SHA-256·크기가 `files` 항목과 같아야 함
   - 모두 끝난 뒤 `files` 의 모든 경로가 나왔는지 확인
7. **적용**: 임시 폴더를 캐시 폴더로 **원자적으로 교체**(새 폴더 이름 바꾸기). `last = {version, published_at}` 저장. `changelog` 를 이용자에게 "규칙이 X.Y.Z 로 업데이트됨" 과 함께 보여 준다.

- 버전이 **낮아진** 포인터(롤백)도 위 검증을 통과하면 그대로 적용한다.
- 캐시가 비어 있는데 검증이 실패하면 작업을 시작하지 않고 오류를 보여 준다.
- 이전 번들 1개는 캐시에 남겨 두어도 된다(오프라인 복구용).

---

## 6. 카테고리 합성

[categories/_catalog.yaml](content/categories/_catalog.yaml) 을 먼저 읽는다.

### 6.1 순서

| 단계 | 입력 | 비고 |
|---|---|---|
| 1 | base: `base/*.md`, `base/checks.yaml`, `base/banned_phrases.txt` | 항상 |
| 2 | `content_types.<type>.defaults` | `<type>` = 메인 카테고리의 `content_type` (`news` \| `info` \| `empathy`) |
| 3 | 메인 카테고리 `categories/<main>/preset.yaml` + 오버라이드 파일 | |
| 4 | 서브 카테고리(0개 이상) `categories/<sub>/preset.yaml` | **`sources`, `trend_keywords` 만** 반영 (`merge.sub_category_keys`) |
| 5 | 계정 개별 설정 `accounts/<계정>/profile.yaml` | 클라이언트 로컬, 번들에 없음 |

### 6.2 병합 규칙 (`merge`)

- **맵**: 키 단위 깊은 병합, 뒤 단계가 이긴다.
- **리스트**: 뒤 단계가 통째로 교체. 단 `merge.append_lists` 의 경로(`sources.rss`, `sources.google_news`, `trend_keywords`, `hashtag_seed`, `risk_notes`)는 **합집합**(앞 단계 순서 유지 + 새 항목 뒤에 추가, 중복 제거. `sources.rss` 는 `url` 기준 중복 판정).
- **오버라이드 md** (`merge.overridable_files` 에 있는 이름만): base 같은 이름 문서 끝에 다음을 덧붙인다(base 를 대체하지 않음).

```
\n\n## 카테고리 추가 규칙 (<preset.name>)\n\n<오버라이드 파일 내용>
```

- `banned_phrases.txt` 오버라이드는 base 목록 뒤에 줄 단위로 덧붙인다.
- 같은 규칙이 충돌하면 더 엄격한 쪽을 따른다(문서 규칙. 수치는 `checks.yaml` 만 쓴다).

### 6.3 `sources` 해석

| 키 | 의미 |
|---|---|
| `rss[]` | `{name, url, lenient?}`. `lenient: true` 면 XML 이 일부 깨져도 관대한 파서로 읽는다 |
| `google_news[]` | 검색어. URL = `_catalog.yaml` 의 `google_news_rss` 에서 `{query}` 를 **URL 인코딩한 검색어**로 치환 |
| `google_trends` | true 면 `google_trends_rss` 를 후보 소스에 넣는다 |
| `official[]` | 피드가 없는 공식 출처 이름(조사 단계 참고용, 수집 대상 아님) |

Google 은 짧은 간격의 연속 요청에 429 를 준다 → Google 요청은 순차로, 1초 이상 간격.

### 6.4 preset.yaml 필드

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | 문자열 | 폴더 이름과 같음 |
| `name` | 문자열 | 표시 이름(카드 `chip` 기본값) |
| `content_type` | `news`\|`info`\|`empathy` | |
| `sources` | 맵 | 6.3 |
| `trend_keywords` | 문자열 배열 | 적합도 판단용 |
| `brief_weights` | 맵 | `recency, coverage, fit, interest, visual` (실수, 합 1.0) |
| `hook_style` | 맵 | `default`(`strong`\|`normal`\|`soft`), `strong`·`normal`·`soft`(설명 문자열) |
| `hashtag_seed` | 문자열 배열 | `#` 없이 |
| `default_template` | `A`\|`B`\|`C` | 7장 variant |
| `palettes` | 배열 3개 | `{name, accent, bg, cta, text}`, 색은 `#RRGGBB` |
| `default_cta` | 맵 | `pitch`, `pitch_em`(선택, pitch 의 부분 문자열), `card_desc`, `after`. 문자열 안 `\n` 은 줄바꿈 |
| `posting_time_hint` | 문자열 배열 | `HH:MM` (KST) |
| `risk_notes` | 문자열 배열 | 리스크 안내 |

카테고리 목록(현재): `politics` 시사·정치, `economy` 경제, `tech` IT·테크, `entertainment` 연예·엔터, `sports` 스포츠, `realestate` 부동산 (news) / `finance` 재테크, `health` 건강·운동, `living` 생활정보, `travel` 여행·맛집, `career` 커리어·직장 (info) / `romance` 연애·관계 (empathy). 목록은 `_catalog.yaml` 의 `categories` 가 기준이다.

---

## 7. `checks.yaml` 키 목록

수치 기준은 이 파일만 쓴다(문서의 숫자보다 우선). **필수** 표시는 validate 가 존재·타입을 보장하는 키다. 나머지는 현재 번들에 있지만 클라이언트는 없을 때 괄호 안 기본값을 쓴다.

| 키 | 타입 | 현재 값 | 뜻 |
|---|---|---|---|
| `hook.max_chars.l1` **필수** | 정수 | 18 | 훅 1줄 최대 글자(공백 포함) |
| `hook.max_chars.l2` **필수** | 정수 | 20 | |
| `hook.max_chars.l3` **필수** | 정수 | 22 | |
| `hook.versions` **필수** | 문자열 배열 | `[strong, normal, soft]` | 훅 버전 순서 |
| `hook.max_punct_per_version` | 정수 | 1 (1) | 버전당 `?` `!` 합계 |
| `hook.allow_emoji` | 불리언 | false (false) | |
| `plagiarism.max_consecutive_eojeol` **필수** | 정수 | 4 | 출처와 연속 일치 어절이 이 값 이상이면 실패 |
| `fact.min_sources` **필수** | 정수 | 3 | 사안당 독립 출처 수 |
| `fact.min_verified_claims` **필수** | 정수 | 3 | 훅 제작에 필요한 확정 항목 수 |
| `number.min_sources` **필수** | 정수 | 2 | 숫자 확정에 필요한 일치 출처 수 |
| `brief.candidates` **필수** | 정수 | 10 | 추천 개수 |
| `brief.lookback_hours.{news,info,empathy}` | 정수 | 72, 336, 336 | 후보 수집 범위 |
| `brief.unverified_score_factor` | 실수 | 0.5 | 검증 필요 후보 점수 배수 |
| `brief.unverified_min_rank` | 정수 | 7 | 검증 필요 후보의 최소 순위 |
| `brief.default_weights` **필수** | 맵 | recency .25, coverage .25, fit .20, interest .15, visual .15 | preset 에 `brief_weights` 가 없을 때 |
| `brief.repeat_window_days` | 정수 | 14 | 같은 사안 재게시 금지 기간 |
| `image.min_short_side_px` | 정수 | 1080 | |
| `image.candidates` | [최소, 최대] | [3, 5] | 사진 후보 수 |
| `caption.hashtags` **필수** | [최소, 최대] | [3, 5] | |
| `caption.body_lines` | [최소, 최대] | [3, 6] | |
| `caption.max_chars` **필수** | 정수 | 2200 | |
| `caption.max_emoji` | 정수 | 3 | |
| `caption.max_dashes` | 정수 | 1 | 줄표(— ―) 수 |
| `caption.max_same_ending_run` | 정수 | 2 | 같은 종결어미 연속 허용 횟수 |
| `caption.max_sources_in_line` | 정수 | 4 | 출처 줄 매체 수 |
| `render.reel.width` **필수** / `height` **필수** | 정수 | 1080 / 1920 | |
| `render.reel.seconds` / `card_until` / `fps` | 정수/실수/정수 | 10 / 7.0 / 30 | |
| `render.feed.width` / `height` | 정수 | 1080 / 1350 | |
| `upload.min_interval_minutes` **필수** | 정수 | 30 | 같은 계정 게시 간격 |
| `upload.min_interval_between_accounts_minutes` | 정수 | 5 | |
| `upload.retry` / `retry_delay_minutes` | 정수 | 1 / 2 | |
| `upload.api_poll_seconds` / `api_poll_timeout_minutes` | 정수 | 10 / 5 | 방식 B 상태 폴링 |

`banned_phrases.txt`: 한 줄에 하나, `#` 으로 시작하면 주석, 빈 줄 무시. 기본은 **부분 문자열 일치**. `re:` 로 시작하면 나머지를 정규식(ECMAScript·Python 공통 문법만 사용)으로 검색한다.

---

## 8. 템플릿

[templates/template_meta.yaml](content/templates/template_meta.yaml) 이 기준이다.

### 8.1 파일과 크기

| 이름 | 파일 | 크기(px) | 용도 |
|---|---|---|---|
| `reel_9x16` | `reel_9x16.html` | 1080×1920 | 릴스 0–7초 정지 카드 |
| `feed_4x5` | `feed_4x5.html` | 1080×1350 | 피드 카드뉴스 표지 |
| `cta_follow` | `cta_follow.html` | 1080×1920 | 릴스 7–10초 팔로우 CTA(3초 애니메이션) |

모든 HTML 은 같은 폴더의 `base.css`, `slots.js` 만 참조한다(외부 URL 없음). 번들을 푼 폴더에서 `file://` 로 연다.

### 8.2 variant

`A` 마커형(사진+하단 텍스트, l2 형광펜) / `B` 패널형(사진 상단+사선 브랜드 패널) / `C` 타이포+캐릭터(사진 없음). `C` 에서는 `photo`·`credit` 을 빈 문자열로 준다.

### 8.3 렌더 절차 (`render_contract`)

1. 브라우저(Chromium 권장) 뷰포트 = 템플릿 크기, `deviceScaleFactor` 1.
2. HTML 을 연다 → `await window.renderSlots({variant, brand, slots})` 호출.
3. `body[data-ready="1"]` 이 될 때까지 대기(이미지·폰트 로드 완료).
4. `.canvas` 요소만 캡처.
5. `cta_follow`: `slots["cta.background"]` 에 `reel_9x16` 캡처 PNG(data: URL 또는 file: URL)를 넣고, 프레임마다 `window.ctaSeek(t)` (t = 0 ~ 3.0초, 30fps 면 1/30 간격) 호출 후 캡처. 실시간 미리보기는 `window.ctaPlay()`.
6. 릴스 MP4 = `reel_9x16` 프레임 7초(210프레임) + `cta_follow` 프레임 3초(90프레임), 1080×1920, H.264, 30fps.

### 8.4 `renderSlots` 인자

```js
{
  variant: "A" | "B" | "C",
  brand: { accent: "#FFD23F", bg: "#0E1A2B", cta: "#FF5A36", text: "#FFFFFF", font: "'Pretendard', sans-serif" }, // 모두 선택
  slots: { "l1": "...", "cta.pitch": "바쁜 아침,\n오늘 이슈만 골라드려요", ... }
}
```

- 텍스트 슬롯은 `textContent` 로만 들어간다(HTML 해석 없음). 줄바꿈은 `\n`.
- 값이 빈 문자열·없음이면 해당 요소를 숨긴다(이미지 슬롯 제외). `handle` 이 비면 핸들 줄 전체를 숨긴다.
- 이미지 슬롯 값은 URL(`file:`, `data:`, `https:`).

### 8.5 슬롯

| 슬롯 | 타입 | 템플릿 | 값 출처·형식 |
|---|---|---|---|
| `l1` `l2` `l3` | 텍스트 | reel, feed | 선택된 훅 줄 (글자 수는 `hook.max_chars`) |
| `chip` | 텍스트 | reel, feed | preset `name` 또는 계정 설정, 6자 이내 |
| `date` | 텍스트 | reel, feed | `MM.DD` |
| `handle` | 텍스트 | reel, feed | `@handle` |
| `credit` | 텍스트 | reel, feed | image.md 출처 표기. variant C 면 빈 값 |
| `photo` | 이미지 | reel, feed | 배경 사진(cover). variant C 면 빈 값 |
| `logo` | 이미지 | reel, feed | 없으면 `cta.name` 앞 4글자를 표시 |
| `character` | 이미지 | reel, feed | variant C 캐릭터(contain). 없으면 강조색 말풍선 도형만 |
| `cta.background` | 이미지 | cta | `reel_9x16` 마지막 프레임 |
| `cta.pitch` | 텍스트 | cta | preset `default_cta.pitch` |
| `cta.pitch_em` | 텍스트 | cta | preset `default_cta.pitch_em` — `cta.pitch` 안 첫 일치 부분을 마커로 강조 (요소 없음, 보조 값) |
| `cta.logo` | 이미지 | cta | 없으면 `cta.name` 앞 4글자 |
| `cta.name` | 텍스트 | cta | 계정 브랜드 이름 |
| `cta.handle` | 텍스트 | cta | `@handle` |
| `cta.card_desc` | 텍스트 | cta | preset `default_cta.card_desc` |
| `cta.button` | 텍스트 | cta | 기본 `팔로우` |
| `cta.button_done` | 텍스트 | cta | 기본 `팔로잉 ✓` |
| `cta.after` | 텍스트 | cta | preset `default_cta.after` |

### 8.6 CSS 변수 (`brand_vars`)

| `brand` 키 | CSS 변수 | 기본값 | 쓰임 |
|---|---|---|---|
| `accent` | `--brand-accent` | `#FFD23F` | 마커·칩·좌측 바·캐릭터 배경 |
| `bg` | `--brand-bg` | `#0E1A2B` | B 패널, C 배경, CTA 로고 배경 |
| `cta` | `--brand-cta` | `#FF5A36` | 팔로우 버튼 |
| `text` | `--brand-text` | `#FFFFFF` | 본문 글자 |
| `font` | `--brand-font` | `'Pretendard','Noto Sans KR','Noto Sans CJK KR',sans-serif` | 전체 글꼴 |

`brand` 는 보통 preset `palettes` 중 하나 또는 계정 설정에서 온다. 템플릿은 이 다섯 변수 외의 CSS 변수를 쓰지 않는다.

### 8.7 레이아웃 메타

| 키 | 값 | 뜻 |
|---|---|---|
| `fonts.family` | Pretendard, Noto Sans KR, Noto Sans CJK KR | 클라이언트가 설치·로드 (번들 미포함) |
| `fonts.weights` | 500, 600, 700, 800, 900 | |
| `safe_zones.reel.top` | 220 | 인스타 상단 UI 영역(px) |
| `safe_zones.reel.bottom` | 400 | 하단 캡션·오디오 영역 |
| `safe_zones.reel.right_bottom` | `{width: 150, y_from: 1000, y_to: 1520}` | 우측 버튼 열 |
| `safe_zones.feed.margin` | 60 | |
| `grid_crop.reel` | `{y_from: 240, y_to: 1680}` | 프로필 그리드 3:4 크롭 |
| `timeline` | `reel_seconds: 10`, `card: [0, 7]`, `cta: [7, 10]`, `cta_duration: 3.0`, `fps: 30` | |
| `timeline.cta_keyframes` | veil 0.0, pitch 0.2, card 0.45, tap 1.65, button_done 1.85, after 2.2 | CTA 내부 시각(초) |

클라이언트가 사진을 고를 때 얼굴·주요 피사체가 세이프존에 들어가지 않게 크롭한다(image.md 4절).

---

## 9. 실패 처리 요약

| 상황 | 동작 |
|---|---|
| 네트워크 오류, 404, 5xx | 현재 캐시 사용, 다음 작업 시작 때 재시도 |
| latest.json 서명 실패 | 5초 후 1회 재시도 → 실패 시 캐시 사용 + 경고 |
| schema ≠ 2, channel 불일치, published_at 역행 | 거부, 캐시 사용, 경고 |
| min_client_version 초과 | 캐시 사용, "플러그인 업데이트 필요" 안내 |
| manifest·번들·파일 해시 불일치 | 거부, 임시 파일 삭제, 캐시 사용, 경고 |
| 캐시 없음 + 위 실패 | 작업 시작 안 함, 오류 표시 |

---

## 10. 참조 코드 (JavaScript / Node 18+)

```js
import crypto from 'node:crypto';

const PUB_B64 = '/OPxSlRxnd/fvMH3VovdNQWEUaSN+PxiLRo2hsctfUo=';
const PUB = crypto.createPublicKey({
  key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), Buffer.from(PUB_B64, 'base64')]),
  format: 'der', type: 'spki',
});

// RFC 8785 호환 (문자열·정수·불리언·null·배열·객체 범위)
const canon = v => Array.isArray(v) ? '[' + v.map(canon).join(',') + ']'
  : (v && typeof v === 'object')
    ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}'
    : JSON.stringify(v);

export function openEnvelope(text) {
  const env = JSON.parse(text);
  if (!env || typeof env !== 'object' || Array.isArray(env) ||
      Object.keys(env).sort().join() !== 'payload,sig' ||
      typeof env.payload !== 'object' || typeof env.sig !== 'string') throw new Error('봉투 형식 아님');
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(env.sig)) throw new Error('sig base64 아님');
  const sig = Buffer.from(env.sig, 'base64');
  if (sig.length !== 64) throw new Error('sig 길이 오류');
  if (!crypto.verify(null, Buffer.from(canon(env.payload), 'utf8'), PUB, sig)) throw new Error('서명 불일치');
  return env.payload;
}
```

이 코드는 3.3 테스트 벡터와 실제 서버의 `bundles/1.0.1.manifest.json` 으로 검증을 마쳤다. 나머지 단계(5장 2–7)는 [tools/client_verify.py](tools/client_verify.py) 의 `verify()` 를 그대로 옮기면 된다.
