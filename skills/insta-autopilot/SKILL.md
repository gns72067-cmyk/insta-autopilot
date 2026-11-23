---
name: insta-autopilot
description: 인스타 카드뉴스·10초 릴스 제작과 업로드 — 주제만 고르면 조사·훅·사진·카드·MP4·캡션까지 자동으로 만들고, 검토 화면(웹)에서 확인·수정·업로드. 계정이 2개 이상이면 일괄 승인(R1~R4). "인스타 만들어줘", "오늘 이슈 추천", "아침 브리핑", "카드뉴스", "릴스", "업로드해", "검토 화면", "승인한 거 올려줘" 같은 요청에 사용. 계정이 없으면 insta-setup 을 먼저 쓴다.
---

# insta-autopilot — 메인 작업

조사·글쓰기는 Claude 가, 수집·검사·렌더·업로드 보조는 플러그인 스크립트가 **이용자 PC 에서** 한다.
**무엇을 어떻게 만들지는 규칙 번들이 정한다.** 이 문서는 순서와 도구만 안내한다 — 수치·금지 표현·형식·화면 절차는 반드시 `rules_compose` 출력에서 읽는다.

## 실행 방법

- 플러그인 루트 `ROOT` = 환경변수 `CLAUDE_PLUGIN_ROOT` (없으면 이 SKILL.md 가 있는 폴더의 두 단계 위).
- 명령: `python "ROOT/scripts/ia.py" <command> ...` (Windows 에서 `python` 이 없으면 `py -3`). 아래에서는 `ia` 로 줄여 쓴다.
- 출력의 `✔ ⚠ ✘ ?` 줄과 종료 코드를 근거로 판단한다. 결과 파일 경로는 이용자에게 그대로 보여 준다(리모트·폰에서 열어볼 수 있게).
- 작업 폴더 `JOB` = `job new` 가 출력한 경로. 모든 산출물은 그 안에 쓴다.
- 모든 질문은 **채팅 안에서 완결**한다: 표 + 미리보기 이미지(카드·그리드 PNG 를 이미지로 보여 줌) + 짧은 응답. 폰 리모트에서도 답할 수 있게.

## 0. 시작 (매 작업마다)

1. `ia rules_sync` — **반드시 첫 단계.**
   - `✔ 규칙이 X 로 업데이트됨: …` → 변경 요약을 이용자에게 **한 줄로** 알린다.
   - 이어서 **이전 작업을 계속하는 경우**(`ia job list` 의 rules 가 X 보다 낮고 업로드 전): "이 작업은 규칙 Y 로 만들어졌습니다. 새 규칙 X 로 바꿔 카드부터 다시 만들까요?" 를 묻는다 ⏸.
     예 → `ia job rebase-rules JOB` → `ia checks hook JOB --selected` (걸리면 훅 단계로 돌아가 다시 고른다) → 카드 단계부터 다시. 아니오 → 기존 버전 그대로 진행.
   - `⚠ … 캐시로 진행` → "규칙 서버 확인 실패, 마지막 검증본 X 로 진행" 을 알리고 계속한다.
   - `✘` (종료 코드 2) → 작업을 시작하지 않는다. 서명 검증에 실패한 규칙은 절대 쓰지 않는다.
2. `ia setup_account show` — 계정이 없으면 insta-setup 으로 안내하고 멈춘다.
3. `ia rules_compose <계정> workflow` 를 읽는다 — 멈춤 지점(⏸)·라운드·응답 문법은 이 문서를 따른다.
4. 모드:
   - workflow 규칙에 **"빠른 모드"** 절이 있으면 기본은 **빠른 모드(Q절)**. 이용자가 "단계마다 확인할게" 라고 하면 단계별(A·B절).
   - 빠른 모드 절이 없는 규칙 버전이면 예전대로: 활성 계정 2개 이상 + `approval_mode` auto/batch → B절, 아니면 A절.
5. 미완료 작업: `ia job list <계정>` (일괄 모드는 `ia batch status`) — 있으면 "이어서 할까요?" 를 먼저 묻는다.

---

# Q. 빠른 모드 (기본) — 주제 선택 ⏸ → 자동 제작 → 검토 화면 ⏸ → 업로드 ⏸

이용자가 할 일은 **주제 고르기, 검토 화면에서 확인·수정, 업로드 버튼** 셋뿐이다. 그 사이에는 멈추지 않는다.

## Q1 주제 선택 ⏸
- 계정 1개: A절 ① 그대로(추천 표 → 번호).
- 계정 2개 이상: B절 R1 그대로(아침 브리핑 → `시사: 1,3 / 연애: 2`). `ia batch r1` 로 작업 생성·공유 팩트 시트 처리.

## Q2 자동 제작 (게시물마다, 멈추지 않음)
1. 조사 → `factsheet.json` → `ia checks fact JOB` 통과까지 (A절 ②-1·2). "근거 부족"이면 **여기서만** 멈추고 ①로 돌아갈지 묻는다.
2. 훅 3버전 → `hooks.json` → `ia checks hook JOB` 통과까지 고친다 (A절 ②-3).
3. 사진: `ia rules_compose <계정> image --job JOB` 의 **검색어 만드는 법**대로 4~6개(구체적 → 넓은 순) → `ia image_search JOB --query … --query …`
   → 모아보기 이미지(`photo_candidates.png`)를 **직접 열어 보고** 사안에 맞는 1장의 번호 N 을 고른다.
   맞는 게 없으면 검색어를 바꿔 **한 번 더** 검색, 그래도 없으면 `--none`(C 템플릿). 관련 없는 사진은 고르지 않는다.
4. `ia make JOB --photo N` (또는 `--none`) → 훅 자동 선택(검사 통과 버전 중 추천) + 사진 + 카드 + 릴스.
   `✘` 면 원인을 고쳐 다시(훅 문제면 hooks.json 수정). 카드 검수 `✘` 가 남아도 멈추지 말고 검토 화면에 표시되게 둔다.
5. 캡션: A절 ⑤-1~4 (스타일 예시가 없으면 묻지 말고 그대로 진행, 검토 때 한 줄로 알림) → `ia caption draft JOB --file …` → `ia checks caption JOB` 의 `✘` 는 고친다.
6. 여러 게시물이면 1~5 를 게시물마다 반복(렌더는 `make` 가 하므로 순서대로).

## Q3 검토 ⏸
1. `ia web open` → 출력의 **주소·접속 코드·바로 열기 주소**를 이용자에게 보여 준다(밖에서 보는 설정이면 휴대폰 주소도 함께).
   한 게시물이면 `ia web open JOB` (그 게시물로 바로 열림).
2. 채팅에는 게시물마다 카드 이미지와 훅 3줄, 캡션 첫 줄만 짧게 — "검토 화면에서 훅·사진·템플릿·캡션을 바꾸고, **캡션 확정 → 이대로 확정 → 업로드** 를 눌러 주세요. 채팅으로 말해도 됩니다."
3. 이용자가 **채팅으로** 고쳐 달라고 하면: 훅 문구·버전 → `ia job select-hook` + `ia checks hook --selected` + `ia render_card` + `ia render_reel` / 사진 → `ia image_fetch --pick` + 렌더 / 캡션 → `ia caption done JOB --from-file`. 훅을 새로 써 달라거나 조사를 더 해 달라는 요청도 채팅에서 처리하고, 끝나면 "검토 화면을 새로고침해 주세요".
4. 검토 화면에서 한 일은 meta.json 에 남는다 — 이용자가 "확인했어", "어떻게 됐어?" 라고 하면 `ia job show JOB` 로 상태를 읽어 답한다.

## Q4 업로드 ⏸
- **검토 화면 업로드 버튼**: 확인창(계정·시각·방식·캡션 첫 줄)에서 한 번 더 눌러야 실행된다(workflow 1절). 방식별 결과:
  - B(api): 서버가 바로 게시하거나 예약 큐에 넣는다.
  - C(manual): 화면에 영상 받기·캡션 복사·게시 주소 입력 칸이 나온다(휴대폰에서 바로 올리기 좋음).
  - A(browser): `upload_browser.json` 이 만들어지고 상태가 "업로드 승인됨 · Claude 진행 대기" 가 된다. 이용자가 "승인한 거 올려줘" 라고 하면,
    `ia job list` 에서 `meta.upload.status == approved_browser` 인 작업을 찾아 A절 ⑥ 방식 A 1~5 를 진행한다(이미 승인됨 — 다시 `업로드` 를 받을 필요 없음, 단 로그인 계정 확인은 매번).
- **채팅**으로 `업로드` 라고 하면 A절 ⑥ / B절 R4 그대로.

---

# A. 단계별 모드 (계정 1개)

## ① 추천
1. `ia rules_compose <계정> brief` → `ia collect_trends <계정>`.
2. candidates.json 의 신호와 기사 제목·링크로 **brief 규칙대로** Claude 가 점수를 매겨 상위 `brief.candidates` 개를 고른다(필요하면 기사 확인). 채우지 못하면 개수와 사유.
3. brief 형식 표 + 1순위 ★ → ⏸ 번호 선택.
4. `ia job new <계정> --title "<요약>" --slug "<슬러그>" --cluster "<candidates.json>#<id>"` → `JOB` (규칙 버전 고정, 이후 rules_compose 는 `--job JOB`).

## ② 조사·팩트 시트·훅
1. `ia rules_compose <계정> fact --job JOB` → 웹 조사 → `JOB/factsheet.json` (sources 에 기사 `title`, 핵심 원문 문장은 `excerpts` 배열로. quote 원문은 `quote` 필드).
2. `ia checks fact JOB` → `✘` 면 보강. "근거 부족"이면 ① 로 돌아갈지 묻는다. → `ia job factsheet-md JOB` → `ia job stage JOB research done`.
3. `ia rules_compose <계정> hook --job JOB` → `JOB/hooks.json` → `ia checks hook JOB` 통과할 때까지 고친다.
4. 통과 버전을 근거와 함께 제시 ⏸ → `ia job select-hook JOB <버전> [--l1 …]` (줄 수정 시 `ia checks hook JOB --selected`) → `ia job stage JOB hooks approved`.

## ③ 사진·카드
1. `ia rules_compose <계정> image --job JOB` → `ia image_search JOB --query "…" --query "…"` → 사안과 맞지 않는 후보는 빼고 제시(모아보기 `photo_candidates.png`). 없으면 C 템플릿 안내 → `ia image_fetch JOB --none`.
2. ⏸ 사진 번호(또는 C) → `ia image_fetch JOB --pick N`.
3. `ia render_card JOB` → `card.png`·`card_grid.png`·`card_guide.png` 이미지를 보여 준다. 남은 `✘` 는 그대로 알린다.
4. ⏸ 카드 컨펌 → `ia job stage JOB card approved`.

## ④ 10초 MP4
`ia render_reel JOB` → ffprobe 검증 결과 표시.

## ⑤ 캡션 → 이용자 직접 수정
1. `ia rules_compose <계정> caption --job JOB` 를 읽는다.
2. `ia caption style-status <계정>` — `⚠ 스타일 예시 0~2개` 면 이용자에게 묻는다:
   "과거 캡션 10~20개를 붙여 넣어 주시거나, 브라우저로 본인 프로필의 최근 게시물 캡션을 읽어 채울까요?"
   - 붙여 넣으면 그 텍스트를 파일로 저장해 `ia caption style-add <계정> --file <파일>` (예시끼리 `---` 줄로 구분).
   - 브라우저를 고르면 Claude in Chrome 으로 `instagram.com/<handle>/` 의 최근 게시물을 열어 **본인 계정 캡션만** 읽고 `--source profile` 로 추가. 다른 사람 계정은 읽지 않는다.
   - 둘 다 원치 않으면 그대로 진행.
3. caption 규칙 + 팩트 시트 확정 항목 + 확정 훅 + style_examples.md 로 초안을 쓴다 → 파일로 저장 → `ia caption draft JOB --file <초안>`.
4. `ia checks caption JOB` → `✘` 는 이용자에게 보여 주기 전에 고친다(→ `draft --force`).
5. 초안 전문과 `JOB/caption.txt` 경로를 보여 주고 ⏸ "이대로 쓰거나, 채팅으로 수정본을 보내거나, PC 에서 caption.txt 를 직접 고친 뒤 '완료'라고 해 주세요."
   - 채팅 수정본 → 파일로 저장 후 `ia caption done JOB --from-file <파일>`
   - 파일 직접 수정·그대로 사용 → `ia caption done JOB`
   - `⚠` 는 **고치지 말고 경고만** 보여 준다. 이용자가 그대로 쓰겠다면 그대로 둔다(업로드 확인에서 금지 표현은 실패 처리됨).
6. 수정 기록은 자동으로 `accounts/<handle>/edit_log.jsonl` 에 쌓인다.
7. 주 1회 개선안: `ia caption suggest <계정> --if-due` 가 결과를 내면 항목 목록을 보여 주고, 이용자가 고른 번호만 `ia caption apply <계정> <json> --items 1,3`.

## ⑥ 업로드 (이용자 본인 계정)
1. `ia rules_compose <계정> upload --job JOB` 를 읽는다(방식 A 화면 절차는 이 출력의 2절).
2. `ia upload preflight JOB [--at <예정 시각>]` → 9항목 표. ❌ 가 있으면 업로드하지 않고 사유를 알린다.
3. 업로드 직전 **한 화면에** 보여 준다: 대상 계정 `@handle`, 첫 프레임(`card.png` 이미지), 캡션 전체(`caption.txt` 최신본), 방식(A/B/C), 예정 시각, 확인 표.
   ⏸ "업로드하려면 `업로드` 라고 입력해 주세요." — **`업로드` 낱말이 있는 응답만** 승인이다. `네`·`OK`·`ㅇㅇ`·`전체 추천대로` 는 승인이 아니다.
4. 이용자 응답 원문을 그대로 넘긴다: `ia upload run JOB --approval "<응답 원문>" [--at <시각>]`.
   리허설은 `ia upload run JOB --dry-run` (승인 불필요).

### 방식 A — browser (기본)
`upload run` 이 `upload_browser.json`(파일·캡션·대상 계정)을 만든다. 그다음 Claude 가 **이용자 PC 의 로그인된 브라우저**에서 직접 진행한다:
1. 도구 선택: **Claude in Chrome**(이용자 Chrome 로그인 그대로) → 파일 첨부가 안 되면 **데스크톱 화면 제어(computer use)** → 그것도 안 되면 **방식 C 로 안내**하고 멈춘다.
2. instagram.com 을 열고 로그인된 핸들을 읽어 `ia upload check-account JOB --seen @<읽은 핸들>`.
   `✘ 중단` 이면 즉시 멈추고 이용자에게 계정 전환을 요청한다(로그인·비밀번호 입력은 하지 않는다). 전환 후 다시 확인.
3. upload 규칙 2절 순서대로: 만들기 → 파일 선택(`reel.mp4`, 브라우저 도구의 파일 업로드 기능) → 릴스 형식·원본 비율 확인 → 다음 → 다음 → 캡션.
   - `--dry-run` 이면 **캡션 입력 직전에 멈추고** 화면을 스크린샷으로 보여 준 뒤 창을 닫거나 취소한다(게시하지 않음).
4. 캡션은 `caption.txt` 최신본을 붙여 넣고 첫 줄·마지막 해시태그를 확인 → `공유하기` 직전 스크린샷 → 공유 → 공유 완료 확인 → 게시물 URL.
5. `ia upload record JOB --url <URL> --method browser --screenshot <파일>`.
- 사람 속도로, 한 번에 한 게시물. 화면이 규칙 절차와 다르면 진행하지 말고 스크린샷과 함께 알린다.
- 여러 게시물이면 `ia upload next-slot <계정> --method browser` 시각 전에는 시작하지 않는다.

### 방식 B — api
`upload run` 이 토큰 확인(+만료 7일 전 갱신) → 계정 일치 → 한도 → 임시 공개 URL → 컨테이너(REELS) → 상태 폴링 → 게시 → 임시 파일 삭제 → permalink 기록까지 한다.
`--at` 이면 예약 큐에 넣고, 예약 작업이 `ia upload due` 로 시각이 되면 게시한다.
토큰 만료·한도 초과면 방식 C 로 바꿀지 묻는다.

### 방식 C — manual
`upload_manual.md`(파일 경로·복사용 캡션·체크리스트·권장 시각)를 보여 준다. 이용자가 게시 후 URL 을 알려 주면 `ia upload record JOB --url <URL> --method manual`.

---

# B. 일괄 승인 모드 — 단계별 (계정 2개 이상)

빠른 모드(Q절)에서는 R1 뒤 R2·R3 를 묻지 않고 Q2(자동 제작) → Q3(검토 화면, 전 게시물) → Q4/R4 로 간다. 아래는 이용자가 단계별을 원할 때.

계정마다 카테고리·브랜드·CTA·업로드 방식이 다르지만 **같은 명령**으로 처리한다(계정 분기 없음). 라운드·응답 문법은 workflow 규칙 2절.
게시물 이름 = 별칭+번호(예: `시사1`). 각 라운드에서 항목마다 추천(★)을 표시하고 `전체 추천대로` 를 받는다. **R4 는 자동 수락 없음.**
응답 해석은 스크립트가 한다: `?` 줄(애매·누락)은 **그 항목만** 되묻고, 나머지는 진행한다. 해석 결과(`✔` 줄)를 짧게 되풀이해 보여 준다.

## R1 이슈 선택
1. `ia batch run_all` → 규칙 동기화 1회 + 활성 계정 전체 후보 수집 (실패 소스는 브리핑에 포함).
2. 계정마다 `ia rules_compose <계정> brief` + candidates.json → brief 규칙대로 10개 → JSON 파일로 저장해 `ia batch set-brief <계정> --file <json>`
   (항목: `no, cluster_id, summary, why, outlets, reaction, risk, star`).
3. `ia batch briefing` → 계정별 섹션의 **아침 브리핑 하나**를 채팅에 보여 준다. ⏸ 예: `시사: 1,3,5 / 연애: 2,4`
4. `ia batch r1 "<응답 원문>"` → 작업 생성. `같은 사안 → 팩트 시트 공유` 묶음은 한 번만 조사하고 `ia batch share-factsheet <원본 JOB> <대상 JOB>...`. 훅·캡션·디자인은 계정별.

## R2 훅·사진
1. 게시물마다 단일 모드 ②(조사·훅 검사)와 ③-1(사진 후보)을 한다.
2. 추천 기록: `ia batch set-rec <라벨> --hook <strong|normal|soft> --photo <번호|C>`.
3. 한 번에 보여 준다: 게시물마다 훅 3버전(근거 claim 요약) + 사진 후보 모아보기 이미지. ⏸ 예: `시사1: 강력, 사진2 / 연애2: 순화, C`
4. `ia batch r2 "<응답 원문>"` → 승인된 게시물 즉시 `ia batch render` (카드+릴스 병렬, `render_concurrency`).
5. 렌더가 끝난 게시물마다 ⑤-1~4 로 캡션 초안까지 만든다.

## R3 결과물
1. `ia batch review` → `output/<date>/review.html` (PC 에서 전 계정 한 화면) — 경로 안내.
2. 채팅에는 게시물마다 카드·그리드 이미지, 릴스 경로, 캡션 전문. ⏸ 예: `시사1 OK / 연애2 캡션 둘째 줄 삭제`
3. `ia batch r3 "<응답 원문>"` → `↻` 재작업 항목만 다시 만들어(캡션 수정 요청이면 그대로 반영 후 `caption done`) 그 항목만 재제시.

## R4 업로드
1. `ia batch r4-table` → 계정 × 게시물 × 방식 × 예정 시각 × 확인 결과 표. ❌ 게시물은 자동 제외(사유 표시).
2. ⏸ 예: `업로드` / `업로드: 시사1, 연애2` / `연애2 보류하고 업로드`.
3. `ia batch r4 "<응답 원문>"` → `→` 줄 순서대로:
   - 방식 B: `ia upload run <JOB> --approval "<응답 원문>" --label <라벨> --at <예정 시각>` (계정별 예약)
   - 방식 A: 같은 명령(--at 없이)을 예정 시각에 하나씩, **매번 로그인 계정 확인**, 계정 바꿔가며 순차·최소 간격.
   - 방식 C: 패키지 안내.
4. 끝나면 `ia batch status` 결과 표(성공 URL·실패 사유)를 보여 준다. 실패 건은 남기고 나머지는 계속.

---

# C. 아침 자동 실행 (예약 작업에서 호출될 때)

프롬프트가 "insta-autopilot 아침 브리핑" 이면:
1. `ia rules_sync` → `ia web start` (검토 화면 켜기, 이미 켜져 있으면 그대로) → `ia ig_api token-refresh-due` (방식 B 계정) → `ia upload due` → `ia perf due` → `ia tempurl cleanup`.
2. 계정이 2개 이상이면 B절 R1 의 1~3, 1개면 A절 ①. 실패 사유(규칙 URL·소스 장애·토큰 만료·로그인 문제)를 브리핑 맨 위에 적는다.
3. 브리핑을 보여 주고 멈춘다 — 이용자가 폰에서 이 세션으로 이어서 답한다.
4. `ia caption suggest <계정> --if-due` 결과가 있으면 브리핑 끝에 한 줄로 알린다. `ia perf report <계정>` 은 월요일에만.

# D. 성과 기록 (선택)
`ia perf due` 목록마다: 방식 B 면 `ia perf fetch JOB --at 24h|72h`, 방식 A·C 면 브라우저로 **본인 게시물의 공개 지표**(좋아요·댓글·조회수)를 읽어 `ia perf record JOB --at 24h --json '{…}'`.
주간: `ia perf report <계정>` → 조정안은 이용자 승인 후에만 profile 에 반영.

# E. 플러그인 업데이트 ("업데이트해줘", "새 버전 받아줘")

규칙(훅 기준·템플릿·사진 규칙)은 작업마다 `ia rules_sync` 로 **자동** 갱신된다 — 이 절은 플러그인 코드(스크립트·검토 화면·이 문서) 업데이트다.
1. Bash 로 `claude plugin marketplace update insta-autopilot` → `claude plugin update insta-autopilot@insta-autopilot` 를 실행한다.
   - `claude` 명령이 없거나 실패하면(데스크톱 앱만 쓰는 PC 등) 이용자에게 안내: 입력창에 `/plugin` → **Marketplaces** → insta-autopilot → **Update**.
2. 끝나면 이용자에게: "입력창에 `/reload-plugins` 를 입력하거나 새 세션을 열어야 새 버전이 적용됩니다." (슬래시 명령은 Claude 가 대신 실행할 수 없다)
3. 새 세션에서 `ia web start` 가 검토 화면을 새 버전으로 자동으로 다시 켠다.
- 자동 업데이트를 켜 두면 다음부터는 1번이 필요 없다: `/plugin` → Marketplaces → insta-autopilot → **Enable auto-update** (기본은 꺼짐).

## 하지 말 것
- 규칙(수치·금지 표현·형식·출처 기준·화면 절차)을 이 문서나 기억에서 가져오지 않는다 — 항상 rules_compose 출력.
- 서명 검증이 안 된 규칙 사용, 언론사·통신사 사진 사용, 기사 문장 베끼기, 팩트 시트 밖 내용·지어낸 인용.
- `업로드` 명시 응답 없이 게시, 다른 계정으로 로그인된 상태에서 게시, 로그인·비밀번호·토큰을 대신 입력.
- 이용자 데이터(설정·결과물·토큰·수정 기록·성과)를 외부로 보내기. 웹 검색에 개인정보를 넣지 않는다.
- ⏸ 지점에서 이용자 확인 없이 다음 단계로 넘어가기.
- 검토 화면 접속 코드를 채팅 밖(웹 검색·외부 서비스)에 쓰기. 검토 화면을 `--where lan`/`tailscale` 로 바꾸는 것은 이용자가 원할 때만.
- 검토 화면이 하는 일(확정·업로드)을 이용자 대신 서버 API 로 누르기.
