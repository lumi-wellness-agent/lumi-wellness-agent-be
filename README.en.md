# lumi-wellness-agent-be

[한국어](README.md) · **English**

> **This file is a translation.** [README.md](README.md) (Korean) is the source of truth; where the two
> disagree, the Korean one is right. LUMI currently runs in Korean only — intake questions, measures,
> activity guidance and rationales are written in Korean. Korean strings that are real values in the
> running system (fixed questions, chip labels, words the rationale rules look for) are kept as they are,
> with an English gloss next to them. Linked documents marked (Korean) have no English version yet.

The backend of LUMI (Light + Mind). LUMI is an AI wellness agent grounded in psychological theory and
clinical/experimental research references: it recommends nature image and sound recovery sessions suited to
the user's state and shows why on a Reference card. It uses FastAPI, Supabase Postgres (pgvector) and
LangGraph · LangChain. The frontend is developed in
[lumi-wellness-agent-fe](https://github.com/lumi-wellness-agent/lumi-wellness-agent-fe).

The project is at the skeleton stage of agent architecture v2. Tests cover the graph wiring (intake →
pre-measure → plan → wait for the activity → post-measure → evaluate → replanning loop → final measure), the
contracts between nodes, the 21 activity node JSON files (filled in by three owners, not yet reviewed), the
contracts and scoring of the 4 measurement instruments, and the evidence contracts (schemas, tool output,
validation rules). 122 evidence rows (61 arms) are loaded into Supabase, where hybrid search and validation
run, and plan has passed three scenarios (S1 · S2 · S3) with a real model (gpt-5.4-mini) (`make plan-trial`).
intake was rebuilt as a State (conversation) → Goal (cue-table inference) → Context (environment chips)
question loop (#49), and its flow is tested with a stand-in model. Goal inference also reads the verbatim
concept definitions from R1 papers (②) and keeps the definition of the inferred goal as evidence. The session
API is in production together with Supabase login, guest trials and session records, and plan builds a
Reference card for every plan. Content generation is not done yet. Who fills in what is in
[TEAM.md](TEAM.md) (Korean).

## Structure

### Offline: building the research evidence (`app/ingestion/`)

```text
collect.py   collect papers — OpenAlex · Europe PMC
parse.py     parse full texts — Docling (PDF) · JATS XML
extract.py   per-experiment Evidence Records + metadata → evidence_records table
```

### Online: LUMI Care Agent (`app/agent/`)

Agent architecture v2 is a single LangGraph graph. `make graph` prints the diagram below.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	intake_read(intake_read)
	intake_ask(intake_ask)
	intake_goal(intake_goal)
	intake_context(intake_context)
	intake_finish(intake_finish)
	measure_pre(measure_pre)
	plan(plan)
	prepare(prepare)
	run_session(run_session)
	measure_post(measure_post)
	evaluate(evaluate)
	replan(replan)
	confirm(confirm)
	measure_final(measure_final)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake_read;
	intake_read -.-> intake_ask;
	intake_read -.-> intake_goal;
	intake_read -.-> __end__;
	intake_ask -.-> intake_read;
	intake_ask -.-> intake_goal;
	intake_ask -.-> intake_finish;
	intake_goal --> intake_context;
	intake_context -.-> intake_ask;
	intake_context -.-> intake_finish;
	intake_finish --> measure_pre;
	confirm -.-> __end__;
	confirm -.-> measure_final;
	confirm -.-> plan;
	evaluate --> replan;
	measure_post --> evaluate;
	measure_pre --> plan;
	plan --> prepare;
	prepare --> run_session;
	replan --> confirm;
	run_session --> measure_post;
	measure_final --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| Node | Architecture | What it does | File |
| --- | --- | --- | --- |
| `intake_read` | intake ① State (LLM) | Reads, from the text (and earlier questions and answers), the safety signal · relevance · user state (before/after an event · after fatigue · no trigger) · event · repetitive-thinking cue · circumstances mentioned in the text, and drafts a question. A safety signal ends the session (`ended = SAFETY`). If the first input is unrelated, it issues an entry re-ask (`restate`) fitted to what the user said. It never estimates scores | `nodes/intake.py` |
| `intake_goal` | intake ③ Goal (LLM) | From the concept definitions (`data/goal_definitions.json`, verbatim), the cue table (`data/intake_cues.json`, team summary) and situational cues, decides the outcome to improve — S1 · S2 · S3 — with a secondary goal and the outcomes to improve (`target_outcomes`). Keeps the definitions of the inferred goal and secondary goal as `goal_evidence` (paper_id · verbatim sentence) (② — about ten sentences, so they go in whole instead of being searched). If both State questions were answered '잘 모르겠어요' ("I'm not sure"), the user pressed '그냥 시작할게요' ("Just start") on a re-ask, or there are no cues, the goal is random among S1 · S2 · S3 (`goal_source = DEFAULT`, no evidence; since 10-29 — before that it was always S2) | `nodes/intake.py` |
| `intake_context` | intake ④ Context (code) | Asks with chips only for the empty fields among setting · earphones · minutes that would change the top-ranked activity | `nodes/intake.py` |
| `intake_ask` ⏸ | intake questions | Entry re-ask · timing question · follow-up question · environment chips. 3 questions in total, at most 2 for State (re-asks are not counted and repeat until a relevant answer comes). If the timing question is answered '잘 모르겠어요', a fixed follow-up is asked. A free-text answer goes back to `intake_read` and is read again, safety included. payload `IntakeQuestion`, resume `IntakeAnswer` (`app/schemas/intake.py`) | `nodes/intake.py` |
| `intake_finish` | intake (code) | Fills empty fields with defaults (10 minutes · a quiet indoor place if setting is unknown) and sets the environment conditions (sound · quiet · screen · outdoors) · Place · why intake ended (`end_reason`) | `nodes/intake.py` |
| `measure_pre` ⏸ | measure (code) | Presents SAM (anchor) + the goal scale (S2 STAI-6 · S1 BSRI · S3 MFVAS, a provisional instrument) to the FE and scores the answers | `nodes/measure.py` |
| `plan` | ① Adaptive Care Planning | A fixed procedure — candidates (code, filtered by goal · state · time · environment) → activity choice (LLM) → evidence search and validation (code — top 5 distinct records) → rationale (LLM) → `CarePlan`. `check_plan` post-checks the choice. A Reference card is built from the validated evidence (code, see “Reference card”). The limitations the rationale must disclose (no evidence · opposite results · adapted stimulus · AI image — the same judgment as the card badges) are decided by code and put into the explanation input, and the rationale is post-checked with character rules (`rationale_rules.py` — other scripts · IDs · statistics · Factor names · years outside the evidence …); on a violation it is asked once more with the reason attached | `nodes/planner.py` |
| `prepare` | ② Care Session Executor | Prepares `ActivityContent` from the protocol's steps · guide text · execution spec (`content_spec`) and assets (photos · videos · audio · coloring templates) (speech synthesis is planned) | `nodes/executor.py` |
| `run_session` ⏸ | ② Care Session Executor | Waits until the FE player runs one protocol and sends a `SessionReport` (completed or not · stop reason · 1–3 NOTICE emotion words) | `nodes/executor.py` |
| `measure_post` ⏸ | measure (code) | SAM at the end of each round. S3 also gets MFVAS (a 3-item mental-fatigue VAS) — it is a round scale, so it is not asked again at the end | `nodes/measure.py` |
| `evaluate` | ③ Outcome Evaluator | Δ = post − pre (SAM valence · arousal; for S3 also MFVAS fatigue · concentration · energy). S1 valence↑ · S2 arousal↓ · S3 fatigue down by 10 points or more | `nodes/evaluator.py` |
| `replan` | ④ Replanning | Code decides which options are available among end · change activity · additional session (the same activity once more) and a rule suggestion — only end after 3 rounds, no change activity when there is no other activity, no additional session for an activity stopped midway (provisional rules) — and the LLM (`NextStepChoice`), starting from the rule suggestion, looks at the stop reason · emotion words · Δ and picks the step to recommend within the options, with a reason. If it fails, the rule suggestion stands | `nodes/replanner.py` |
| `confirm` ⏸ | ④ Replanning | Waits for the user's choice and consent (`ReplanDecision`). Change activity or an additional session goes back to `plan` (the next round's pre value = this round's post value). Change activity removes the activities the user moved away from for the rest of the session; an additional session does not choose again — same activity, new content only | `nodes/replanner.py` |
| `measure_final` ⏸ | measure (code) | On the end path, the goal scale measured only at pre and end (STAI-6 · BSRI), once → session Δ. S3 is skipped because it is measured every round | `nodes/measure.py` |

⏸ marks a LangGraph interrupt. The graph stops and leaves its state in the checkpointer; when it receives
`Command(resume=value)` on the same thread_id, it continues from that point. A node with an interrupt runs again
from the top when resumed, so costly work such as content generation finishes in the node before it
(`prepare`) and stays in the state.

| Goal (outcome to improve) | Desired change | Goal scale | Main activity nodes |
| --- | --- | --- | --- |
| S1 rumination relief | Step away for a while from thoughts that keep coming back | BSRI (Korean translation draft) | PR-09 mindfulness / PR-21 image + meditation (experimental) / PR-15 coloring (experimental) |
| S2 tension · stress relief | Ease stress and tension | STAI-6 (Korean translation draft) | PR-06 breathing / PR-02 video + nature sounds / PR-01 nature photos … |
| S3 attention · fatigue recovery | Recover attention after cognitive fatigue | MFVAS (mental-fatigue VAS, provisional and unvalidated — measures subjective fatigue only) | PR-10 ReST (experimental) / PR-11 nature walk / PR-01 nature photos |

A goal is an outcome label, not a bundle of media. The 21 activity nodes live in `data/protocols/`, and one is
chosen per round. Combinations exist only as nodes themselves; the planner never bundles activities on its own —
there are validated multimedia protocols whose combination is the experimental stimulus itself (PR-02 · 12), and
nodes without evidence for their composition (PR-10 · 15 · 21 · 22) are `EXPERIMENTAL` and shown as '실험적'
(experimental).

Common principles

- Paper RAG is for finding evidence; sessions run only reviewed protocols.
- AI-generated images are supporting content, and how they differ from the research stimulus is disclosed on
  the Reference card as `ADAPTED_STIMULUS`.
- For content Factors (scenery · nature sound · artwork subject), the LLM picks only evidence-backed Enum values,
  and code picks the search terms from a reviewed list (see “Content Factors” below).
- Measurement is not RAG. Scales (`data/instruments/`) live in structured tables, and the LLM neither writes
  items nor computes scores.
- LUMI makes no claim of medical diagnosis or treatment efficacy.

### Content Factors (`data/content_factors.json` · `app/content/factors.py`)

plan chooses what fills the content of nature-stimulus activities (PR-01 02 03 04 05 20 21, plus PR-10 · 22,
which use Pixabay scenery). No LLM call is added: plan's LLM call 2 (the rationale) returns `content_factors` in
the same structured output.

| Field | Enum (`app/schemas/content.py`) | Evidence (paper · page per value in the catalog) | Used by |
| --- | --- | --- | --- |
| `scenery` scenery, 0–4 | forest · lake river · sea · mountain · meadow | P03 · P24 · P25 · P28 · P33 · N17 | photos (PR-01 · 03 · 21 · 22), video (PR-02 · 10 — forest · lake/river only) |
| `soundscape` nature sound, 1 | birdsong · stream · rain · ocean_waves · forest_wind | N14 · N15 · N16 · N17 · N18 | PR-02 · 04 |
| `art_subject` artwork subject, 1 | waterscape · landscape | E06 · E07 | PR-20 |
| `music` relaxing-music recommendation, 1 | miserere · mozart_k448 · vivaldi_spring | E09 · E08 (table 20 · p. 23) | PR-05 — the recommended piece goes first and the user chooses (E08: music the user picked from a list, g = 0.38, 1 paper) |

- **Enum = only categories used as stimuli in papers (`basis: STIMULUS`).** Flowers · sky, kept for preference
  without evidence (`DESIGN`), come only from FE chips. Values with evidence that may still be unpleasant, such
  as insect sounds (N16), are kept in `excluded` with the reason.
- **The LLM picks values; code picks search terms.** Each value has 2–3 reviewed search terms (within the
  include · exclude tags), and prepare picks one at random per session, so the same scenery brings different
  photos · sounds each time. For nature sounds, per-value excludes (harsh birdsong etc.) are added to the
  protocol's excludes.
- **Priority:** user chip (`profile.preferred_scenery`) > LLM Factor > the protocol's default search terms.
  `factors.sanitize` drops fields the activity lacks, values without evidence and excess values, and PR-02, which
  uses video and sound together, matches the sound to the video's scenery (forest → birdsong · forest wind,
  lake/river → stream).
- The asset's `metadata.content_factor` records the axis · value · source (`USER` · `LLM`) · evidence paper ·
  search term. `preferred_scenery` is recorded only for user chips, so the FE never labels an LLM-chosen scenery
  as "좋아하는 ○○" ("your favorite ○○").
- Factors personalize within the range of research stimuli; they are not evidence of a larger effect. They are
  not used in the rationale either.
- **Color preferences (profile.preferred_colors · avoided_colors)**: with several photos, about 1 in 4 is fetched
  with Pixabay's color filter for a preferred color (`preferred_color` metadata); an avoided color removes, from
  the LLM's scenery, sceneries dominated by that color (blue → sea · lake/river). Scenery from user chips is kept.
  The table is the catalog's `colors`; there is no evidence that colors affect emotion.
- **Image proxy (`/media`, `app/api/media.py`)**: museum IIIF (Cloudflare blocks direct browser calls) and Pixabay
  photos (permanent hotlinking forbidden) are fetched and relayed by the BE. The asset URL becomes `/media/…` and
  the original URL stays in `metadata.origin_url`. Upstream URLs come from fixed templates, so it is not an open
  proxy (`app/content/proxy.py`).

### Where LangGraph and LangChain are used

- **LangGraph — the whole session graph.** It needs the Replanning → Planner cycle, branching on the user's
  choice, and interrupts that pause until the user finishes an activity or consents and then continue. State
  stays in the checkpointer, so a session continues across separate HTTP requests.
- **LangChain — structured output for intake · plan · replan.** All five LLM calls are a single
  `with_structured_output` (json_schema) call each — intake's situation reading `StateReading` and goal
  inference `GoalInference`, plan's activity choice `ActivityChoice` and rationale `PlanRationale`, and replan's
  next-step recommendation `NextStepChoice`. Whether intake asks, option validation, answer merging and ending are
  code (`app/agent/intake_rules.py`) — however many questions are asked, the LLM runs twice, plus one more reading
  only when a timing question gets a free-text answer. plan is a fixed procedure: code filters the candidates by
  goal · user state · time · environment (sound · quiet · screen · outdoors) and lines them up in grade-weighted
  draw order (`protocols.recommendation_order` — seeded by the session ID; by the best grade on the goal's primary
  outcome A 4 · B 3 · C 2 · otherwise 1; an activity done recently × 0.3); the LLM sees the first 5 with their rank
  and picks rank 1 (moving to the next one only for a constraint · place · an activity the user is reluctant
  about · the previous rejection reason — matched against free text); code searches and validates evidence by
  medium · user state · goal outcome within that activity's `evidence[]` (or, if no record fits, across all
  studies of the same medium) and keeps only the usable ones; and the LLM writes the rationale from that evidence.
  It began as a `create_agent` tool loop, but gpt-4o-mini skipped the search, dropped arguments, and once ran for
  over 10 minutes (measured 2026-10-26), so the procedure moved into code — termination is guaranteed and model
  calls fell from 4–5 to 2. Adaptation comes not from a loop inside this node but from the measure → evaluate →
  replan → plan cycle. `check_plan` post-checks the choice and, on a violation, asks once more with the rejection
  reason. The model is unified on OpenAI `gpt-5.4-mini` (`LLM_MODEL`), and `claude-haiku-5-5` is registered as a
  fallback when an Anthropic key is present (`FALLBACK_MODEL`, `app/agent/assembly.py`). The call rules
  (`app/agent/llm.py`) follow cure-agent-be — 5xx and network errors retry once on the same provider, 4xx falls
  back immediately without a retry, and if everything fails, `LlmExhaustedError`. The vendor SDKs' own retries
  are turned off.
- **Everything else is plain Python.** Measurement and scoring, the protocol loader, the Outcome Evaluator, the
  Replanning rules, and paper collection and search (psycopg) do not use LangChain.

The evidence contracts (`app/schemas/evidence.py` metadata · `protocol.py` activity node JSON · `retrieval.py`
tool output) were derived from the actual values in the 65-row evidence-arm sheet and the activity node JSON, and
the contract tests (`tests/schemas/`) check the conversion for every distinct value in the sheet.

### Session API (`app/api/sessions.py`)

One session is one LangGraph thread. Each request resumes the graph where it stopped (an interrupt), runs it to
the next interrupt (or the end), and returns where it stopped. The shapes of interrupt payloads and resume values
are in each node's docstring.

| Request | Graph | Response `step` |
| --- | --- | --- |
| `POST /sessions` — `CareRequest` (text + environment chips + session language `lang`) | new thread → intake → stops at `intake_ask` if a question is needed (a re-ask if the text is unrelated), otherwise at `measure_pre`. A safety signal ends it with `ended = SAFETY` | `ASK` · `MEASURE` (pre) · `END` |
| `POST /sessions/{id}/answer` — `IntakeAnswer` | resume → stops at the next question or `measure_pre` (3 questions in total, re-asks excluded). A re-ask's `{"skip": true}` starts with a goal chosen at random among S1 · S2 · S3. A safety signal in a free-text answer ends it with `ended = SAFETY` | `ASK` · `MEASURE` (pre) · `END` |
| `POST /sessions/{id}/measure` — `{instrument_id: {item_id: value}}` | pre: resume → plan → prepare → stops at `run_session` | `RUN` |
| `POST /sessions/{id}/report` — `SessionReport` | resume → stops at `measure_post` | `MEASURE` (post — SAM, plus MFVAS for S3) |
| `POST /sessions/{id}/measure` — post answers | resume → evaluate → replan → stops at `confirm` | `CONFIRM` |
| `POST /sessions/{id}/decision` — `ReplanDecision` | resume → for change activity or an additional session, plan → … → `run_session`; for end, `measure_final` (S3 ends right away) | `RUN` · `MEASURE` (final) · `END` |
| `POST /sessions/{id}/measure` — final answers (S1 BSRI · S2 STAI-6) | resume → `measure_final` scoring → end | `END` |
| `POST /sessions/{id}/retry` | reruns a `STALLED` session from the node where it stopped | same as above |
| `GET /sessions/{id}` | read only — page refresh · recovery from a dropped stream | where it is stopped now |

The response body is `{session_id, step, node, payload}`. `step` is derived from the node where the graph
stopped — `intake_ask` → `ASK` (question `IntakeQuestion`, `app/schemas/intake.py`), `measure_*` → `MEASURE`
(`payload.measure` is `PRE` · `POST` · `FINAL`), `run_session` → `RUN` (the plan with its Reference card ·
content), `confirm` → `CONFIRM` (evaluation · suggestion · `options`), the end → `END`. Node names are part of
the API contract, so renaming one means changing the session API too. `END`'s `payload.reason` is `DONE` or the
reason intake ended the session (`SAFETY`); for `DONE` it carries a round summary and the session change in the
goal scale (`goal_scale` — end − pre; for S3 the last post value). If a node fails or the stream drops and the
graph stops between interrupts, the session is `STALLED` and continues with `retry`. A session whose other request
is still running also looks `STALLED` (it is between interrupts), so the FE calls `GET` only on first load and to
recover from a dropped stream.

**RUN payload.** `payload` is `{plan, content}`. In `content` (`ActivityContent`), `steps` · `guide_text` ·
`notice_prompt` are common to all activities, and `assets` holds the assets per slot (photos · videos · audio ·
coloring-template SVGs — repository assets are served by the BE at `/assets/…`). How each activity runs is in
`content.content_spec`, the activity node JSON's `content_spec` passed through as is — scripts per slot
(`slots[].scripts`) and their playback timing (`render`), how things are chosen (`selection`), and the screen
spec (`session_ui` — timer · controls · completion condition · pre-start safety check). Its shape differs by
activity, so the BE does not fix a type. Activities with a NOTICE step (PR-01 · 09 · 15 · 21) also get the
input-popup spec of the step module PR-16 as `content.notice_module`. Voice guidance is not recorded (only PR-21
has a recorded track). For voice-guide slots (`GUIDE_AUDIO` — `generation.provider` `BROWSER_SPEECH_SYNTHESIS`),
the FE shows the script on screen at its `render` time and reads it with the browser's `speechSynthesis` (ko-KR,
one voice per activity). The outdoor activities (PR-11 · 12) are read the same way, as their
`session_ui.tts_policy` says. The opening guide without a playback spec (`guide.intro_text`) is shown on screen
only, and scripts whose `source_policy.tts` is `FORBIDDEN` are not read. The end chime (`countdown_sound` —
PR-06 · 08 · 09 · 22) has no audio file either, so the FE plays a short tone with Web Audio. When the activity
ends, the FE sends a `SessionReport` (`completed` · `stopped_reason` · `notice_words`) to `report`.
`notice_words` are the 1–3 emotion words written in the NOTICE popup (empty boxes dropped, 20 characters each at
most), kept on the server as real-use data.

**SSE.** Adding `Accept: text/event-stream` to a POST streams the same run as events — `session`
(`session_id`) → `progress` (`{node}` when a node finishes — for intake `intake_read` · `intake_ask` ·
`intake_goal` · `intake_context` · `intake_finish`; `{node: "plan", step}` when a step inside plan starts —
`choose_activity` · `search_evidence` · `verify_evidence` · `write_rationale`) → `result` (the same body as the
JSON response). A failed run ends with `error` (shaped like the error body), and a `: ping` comment is sent after
15 seconds without events. Request validation errors (404 · 409 · 422) come back as JSON before the stream opens.
The FE uses SSE only for calls where an LLM runs before the response (`POST /sessions` · `answer` · pre
`measure` · `decision` · `retry`) and JSON for the rest. The browser `EventSource` supports GET only, so the
stream is read with fetch.

**Session language.** `CareRequest.lang` (`ko` by default · `en`) is the session language. Sentences the code
writes — intake fixed questions and options · fixed re-ask sentences · environment chips · goal reasons written by
code (`app/agent/intake_texts.py`), measurement items (`data/instruments/en/`), and the Reference card's goal name ·
limit sentences · scale notes (`app/agent/card_texts.py`) — are sent in this language. The values an option fills
in and the measurement scoring are the same in every language. The LLM calls that write sentences shown to the
user (① state reading's re-asks · question drafts, ③ goal reasons, rationales, next-step recommendation reasons)
add an output-language note (`prompts.*_EN_NOTE`) to the end of the Korean instructions in an English session and
use output models (`*En`) that differ only in their field descriptions — the LLM input of a Korean session is
unchanged. The rationale character rules also check English sentences (see “Rationale evaluation”). The English
version is in progress — activity node wording and evidence metadata are still Korean, and the FE does not send
`lang` yet. A request without `lang` is Korean, the same session as before.

**Errors.** The body is `{"detail": {code, message, …}}`.

| code | Status | When |
| --- | --- | --- |
| `UNAUTHENTICATED` · `INVALID_TOKEN` · `SESSION_REVOKED` | 401 | The login token is missing, its signature · expiry · project does not match, or the login session ended through logout · account deletion (see “Auth · session records”) — the FE sends the user to log in again |
| `CONSENT_REQUIRED` | 403 | A member calls `POST /sessions` without records consent for the current version (`version`) |
| `MEMBERS_ONLY` | 403 | A guest calls `PUT /me/consent` · `GET /me/sessions` — guest trials are not recorded |
| `INVALID_RANGE` | 422 | In `GET /me/sessions`, `since` is later than or equal to `until` |
| `SESSION_NOT_FOUND` | 404 | A session that does not exist, or someone else's (its existence is not revealed either) |
| `SESSION_EXPIRED` | 410 | An unfinished session left idle so long that the cleanup job deleted its checkpoint — start a new one |
| `STEP_MISMATCH` | 409 | A request that does not match where the session is stopped. `step` · `node` are included |
| `INVALID_ANSWER` | 422 | A measure answer with a missing instrument · item or an out-of-range value; an answer to a question other than the current one, or a field · value · free text the question does not accept; a decision not in `options` |
| `PLAN_REJECTED` | 422 | Planning failed. If `retryable`, choose again with `retry` (not when there are no candidates) |
| `LLM_UNAVAILABLE` | 503 | All model calls failed (`retryable`) |
| `AGENT_UNAVAILABLE` | 503 | The session graph was not assembled because there is no LLM key |
| `AUTH_UNAVAILABLE` | 503 | `SUPABASE_URL` is missing, Supabase's public keys (JWKS) could not be fetched, or the login session could not be checked |

The API validates resume values before they enter the graph. If a node raises after an interrupt, LangGraph keeps
that resume value in the checkpoint, so resuming the same interrupt again fails with the first value (even with an
interrupt id). So measure answers are screened first with the same scoring function as the node, intake answers
with the same rule (`intake_rules.apply_answer`), and decisions against `options`. FE origins are allowed by
`CORS_ORIGINS` (default: local 3000 · production FE) and `CORS_ORIGIN_REGEX` (default: FE preview deployments).

The checkpointer stores session state in the `langgraph` schema of Supabase Postgres (`app/agent/checkpoint.py`).
The same session continues even when each request lands on a different server (serverless instance). With an
empty `DATABASE_URL` it uses `InMemorySaver`, so in-progress sessions disappear when the server restarts. A
finished session's checkpoint is deleted right away (see “Auth · session records” below).

### Auth · session records (`app/auth.py` · `app/records.py`)

The FE logs in with Supabase Auth (Google · Kakao) and attaches the access token to every session API request as
`Authorization: Bearer <token>`. The BE verifies the signature with the public keys (JWKS, ES256) of the
`SUPABASE_URL` project and checks expiry · `aud` (`authenticated`) · `iss`; `sub` is the user ID
(`auth.users.id`). Symmetric-key (HS256) tokens are rejected. With an empty `SUPABASE_URL`, the session API does
not open without auth — it returns 503. Only the user who created a session can see it; someone else's session is
a 404, the same as a missing one.

**Without login (guest trial).** A user who does not log in gets a one-off trial that leaves no records. They see
no login screen; Supabase anonymous sign-in creates an anonymous user (`is_anonymous`) in `auth.users`, so token
verification and ownership checks work as they do for members.

| | Member | Guest |
| --- | --- | --- |
| Start | social login → consent screen → `POST /sessions` | `supabase.auth.signInAnonymously()` → straight to `POST /sessions`. One line of notice instead of a consent screen |
| What stays on the server | session records · rounds · (with consent) raw text | only the owner and progress in the session row (`step` · end reason · END payload). Classification values · chips · measurements · rounds · raw text are not written |
| When it is deleted | account deletion (`DELETE /me`) · raw-text retention period | after a day (`GUEST_RETENTION_DAYS`) unused, the cleanup job deletes the whole user |
| Logout · account deletion screens | yes | no |

- Example notice: "체험 내용은 기록으로 남기지 않으며, 진행에 쓴 데이터는 1~2일 안에 지워집니다." ("Your trial is not
  kept as a record, and the data used to run it is deleted within 1–2 days.") Browsers do not guarantee deletion
  at the moment a page is left, so leftover rows and checkpoints are deleted by the daily cleanup job. That input
  goes to OpenAI is stated in the privacy policy, as for members.
- The FE keeps the guest session in `sessionStorage` (supabase-js `auth.storage`) — closing the tab ends the
  trial.
- For guests, `PUT /me/consent` · `GET /me/sessions` return 403 (`MEMBERS_ONLY`). To keep records, a guest
  becomes a member through social login after the trial (the guest trial does not carry over).
- Unused means that the period has passed since creation, since the last login-session refresh and since the last
  session-row write — all three.

Turn on Anonymous Sign-Ins in the Supabase dashboard. Anonymous sign-in is rate-limited per IP (Rate Limits), so
raise the limit beforehand if many people will join from the same Wi-Fi at a demo venue.

Even with a valid signature, a request is refused if its login session has ended (401 `SESSION_REVOKED`). On every
request the token's `session_id` is looked up by primary key in `auth.sessions` of the same DB — logout · logging
out all other devices · account deletion make Supabase delete that row, so access stops immediately instead of
waiting for the access token to expire (1 hour by default). Refreshing the token keeps the same `session_id`.
Supabase manages the `auth` schema, so only `id` · `not_after` are read; if the check cannot be made, the request
is blocked with 503. A server started without `DATABASE_URL` does not check sessions.

Account deletion is `DELETE /me` (204). Deleting the Supabase Auth user row (`auth.users`) removes Supabase's
login sessions · linked identities · MFA and the records · raw text · consent below, all through
`on delete cascade` (production DB constraints checked 2026-10-28), and the user's checkpoints are deleted too. It
cannot be undone, so the FE shows a confirmation screen; on 204 it clears the session left in the browser
(`supabase.auth.signOut({ scope: "local" })`) and goes to the first screen.

Members give two consents. After social login the FE checks `GET /me/consent`; if `agreed` is false, it shows the
consent screen and sends `PUT /me/consent` (guests are not asked).

| Consent | Body | What it covers | Without it |
| --- | --- | --- | --- |
| Session records (required) | `"records": true` | situation classification · question slots and chip answers · measurements · activities · results. They include psychological-state measurements, so they are treated as sensitive information and consented to separately | `POST /sessions` returns 403 (`CONSENT_REQUIRED`). It cannot be sent as false |
| Raw-text retention (optional) | `"raw_text": true` | the first input · free-text answers and their question sentences · PR-16 emotion words · stop reasons, and text derived from them (intake's event · expressions · summary · reasoning, the rationale) | no raw text is kept. Switching to false deletes the kept raw text right away |

The consent wording lives in the FE; the BE only matches the version (`records.CONSENT_VERSION`) — changing the
wording means bumping the version, and users must consent again before starting a session. On every request the
session API rewrites the records from the state after the graph stops.

| Table | Contents |
| --- | --- |
| `user_consents` | one row per user — required-consent version · time, raw-text consent |
| `care_sessions` | one session (id = thread_id) — where it is stopped (`step` · `node`), session language (`lang`), goal, environment chips, intake classification values (`records.INTAKE_FIELDS`), question slots and chip answers, pre · final measurements, end reason and END payload |
| `care_rounds` | finished rounds — activity · minutes · evidence IDs · relaxed conditions, completion, pre · post measurements and change, recommended step · options · user's choice |
| `care_texts` | raw text (only for users who consented to raw text) — one row per session, retention deadline `expires_at` (90 days by default, `TEXT_RETENTION_DAYS`) |

- A session that ended with SAFETY keeps only the fact that it ended (`end_reason`) — chips · classification
  values · raw text are deleted (a table constraint enforces this).
- The session language (`lang`) is not content, so guest and SAFETY sessions keep it too. Completion · stop
  reasons · next steps · activity mix are pooled and also broken down by language; scale scores · scale validation
  (MFVAS reliability) · intake and safety-judgment quality are analyzed per language only.
- When a session ends, its checkpoint, which holds raw text, is deleted right away, and later
  `GET /sessions/{id}` calls are answered from `care_sessions.end_summary`.
- The cleanup job `GET /internal/cleanup` (Vercel Cron, daily at 03:00 KST, `CRON_SECRET`) deletes expired raw
  text, unused guests (one day, `GUEST_RETENTION_DAYS`), checkpoints of unfinished sessions idle for more than
  24 hours (`SESSION_IDLE_HOURS`; the session expires — 410), and checkpoints with no record (sessions from before
  auth · deleted users).
- Every row is deleted along with `auth.users` (`on delete cascade`). Deleting a user from the dashboard also
  removes their records · raw text · consent, and the next cleanup job deletes leftover checkpoints.

Members see their activity history with `GET /me/sessions` (the FE records tab). It returns sessions with at least
one activity (a round record), newest first, up to `limit` (20 by default · 100 at most); sessions that stopped
before an activity or ended with SAFETY are left out. It reads only the analysis tables (`care_sessions` ·
`care_rounds`) and carries no raw text.

The FE records tab is a calendar, so it asks one month at a time. Given ISO 8601 times with a time zone for
`since` (inclusive) · `until` (exclusive), it returns only sessions started in between (e.g. November in Korea
time = `since=2026-10-31T15:00:00Z&until=2026-11-30T15:00:00Z`). Day boundaries are set by the device's time
zone, so a time without a zone is a 422, and `since` later than or equal to `until` is a 422 (`INVALID_RANGE`).
`first_started_at` is the start time of the earliest record regardless of the range (`null` if there is none), so
the FE knows how far back month navigation goes and whether there are any records at all.

```json
{"first_started_at": "2026-10-21T09:40:11+00:00",
 "sessions": [{"session_id": "…", "started_at": "2026-10-29T05:12:03+00:00",
  "ended_at": "2026-10-29T05:24:41+00:00", "goal": "S2",
  "rounds": [{"round": 1, "protocol_id": "PR-06", "name": "주기적 한숨 호흡 5분", "minutes": 5,
              "completed": true, "deltas": {"valence": 1.0, "arousal": -2.0}, "improved": true,
              "next_step": "END"}],
  "goal_scale": {"instrument_id": "INS-STAI6", "pre": {"total": 53.33}, "end": {"total": 43.33},
                 "delta": {"total": -10.0}}}]}
```

- `ended_at` of `null` means an unfinished session (in progress, or expired after idling). `goal_scale` (the
  session change in the goal scale) comes from the END payload of sessions that ended with DONE.
- `rounds` has the same fields as the END payload's `rounds` plus the activity name (`name` — the activity node
  JSON's `name`, Korean for now: 주기적 한숨 호흡 5분 is "cyclic sighing, 5 minutes"; `null` for a removed
  activity). The END payload carries only `protocol_id`, so the FE would not otherwise know past activities'
  names.

### Reference card (`app/agent/reference_card.py`)

The card that answers "왜 추천했나요?" ("Why was this recommended?"). The plan node builds one per plan into
`CarePlan.reference_card`, and the FE draws `plan.reference_card` from the `RUN` payload as is (contract:
`app/schemas/card.py`). It uses no LLM and does not rely on the rationale (`rationale`) sentences — every fact on
the card comes from validated evidence (search results + verdicts), the protocol and the intake result. Research
results are never stretched into diagnosis or treatment effects.

| Field | Contents |
| --- | --- |
| `protocol_id` · `name` · `minutes` · `composition` | the chosen activity |
| `badges` | screen badges (`CardBadge`) — `EXPERIMENTAL` · `LIMITED_EVIDENCE` · `NO_VERIFIED_EVIDENCE` · `CONFLICTING_EVIDENCE` · `ADAPTED_STIMULUS` · `AI_IMAGE_UNVERIFIED` · `RELAXED_ORDER` · `RELAXED_ENVIRONMENT` · `DEFAULT_GOAL` · `PROVISIONAL_MEASURE`. The FE decides wording · color; the explanatory sentences are in `limits` · `goal.notes` |
| `goal` | goal and label, how it was decided (`INFERRED` · `DEFAULT` · `CHIP`) and the reasoning, **goal evidence** (verbatim concept definitions of the inferred goal · secondary goal · concept · page · paper), the primary outcomes the evidence looks at, the scale LUMI measures with (`measured_by`; `provisional` for a provisional instrument), `notes` (default goal · S3 "attention capacity is not measured" · provisional scale) |
| `grades` | this activity's evidence grade per primary outcome of the goal (protocol sheet; A–D · — · unconfirmed) |
| `evidence[]` | one validated experiment each (METHODS · RESULTS chunks of the same experiment grouped; reviews without an experiment ID per paper) — theory → paper (title · authors · year · DOI link) → population · sample · exposure time · study condition → results on the goal outcomes (scale · comparison · time point · statistics verbatim) → whether it supports the goal · flags · limitation sentences, and the source passages (`excerpts`, original English). The reasons from the 6 validation checks (log sentences) are not included — failed non-blocking checks are disclosed as flags · limitation sentences. Evidence supporting the goal comes first |
| `limits` | limitation sentences at the end of the card — a fixed notice, one sentence per badge (the protocol author's `reference_card_note` when present), and the protocol's population note |

- `EXPERIMENTAL` is added when `composition` is `EXPERIMENTAL` or the protocol's `user_facing_flags` call it
  experimental for that goal; `ADAPTED_STIMULUS` when the protocol declares an adaptation (script · audio) or every
  piece of evidence supporting the goal used a different stimulus; `AI_IMAGE_UNVERIFIED` when the user chose
  AI-generated images and they are among the delivered media.
- For S3, the evidence's primary outcome (attention tasks) differs from what LUMI measures (a mental-fatigue VAS,
  a provisional instrument made by the team), which `goal.notes` discloses.
- If the goal scale is `PROVISIONAL`, one sentence per scale goes into `goal.notes`
  (`provisional_notes` in `card_texts`) — for STAI-6 · BSRI, that they are translation drafts of validated English
  versions and the translations have not been validated separately; for MFVAS, that it consists of LUMI's own
  questions and is unvalidated. `PROVISIONAL` follows the items in the session language: in an English session
  STAI-6 · BSRI are the original items, so they get no badge or sentence, and only MFVAS is disclosed.
- Sentences the code writes (goal name · fixed limit sentences · scale notes) are in the session language
  (`app/agent/card_texts.py`). Sentences written in the protocol (name · population note · `reference_card_note`)
  and evidence metadata are still Korean.
- With no evidence DB, or no evidence that passed validation, `evidence` is empty and `NO_VERIFIED_EVIDENCE` is
  added.

## Getting started

Requires Python 3.12 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # fill in the DB · LLM keys and the content provider keys you need (blank providers are disabled)
make run                # http://127.0.0.1:8000/health
```

| Command | What it does |
| --- | --- |
| `make run` | dev server (restarts on code changes) |
| `make lint` | ruff check |
| `make test` | pytest (graph flow and Planner wiring are checked with scripted models, so no LLM is called) |
| `make test-intake` `test-measure` `test-planner` `test-protocols` | per-role tests ([TEAM.md](TEAM.md), Korean) |
| `make graph` | prints the session graph as Mermaid |
| `make protocols-scaffold XLSX=<workbook>` | builds activity node JSON drafts from the protocol · evidence-arm sheets (skips existing files) |
| `make checkpoint-setup` | creates the session checkpoint tables in `DATABASE_URL` (safe to rerun) |
| `make plan-trial ARGS="S2 --repeat 3"` | runs intake → plan with a real model to show the chosen activity · search query · evidence · Reference card (badges · evidence experiments · limitations) (LLM cost, evidence DB read-only). `--queries <jsonl>` collects the search queries |
| `make rag-eval ARGS="run --misses"` | evidence retrieval evaluation — Hit@K · Recall@K · MRR · CP@5 (RAGAS Context Precision) · Foreign@5 over the 180 cases in `data/rag_eval.jsonl` (evidence DB read-only, query-embedding cost). `collect` gathers queries from scenarios (LLM cost), `build` makes the cases, and `compare` compares two results (see “Evidence retrieval evaluation”) |
| `make plan-eval ARGS="--database-url …"` | plan evaluation — runs the plan node with a real model for each of the 30 scenarios, judges it by rules (plan · first choice · candidates · relaxed conditions · check_plan · activity change) and counts Reference card evidence (LLM cost, evidence DB read-only) (see “Plan evaluation”) |
| `make rationale-eval ARGS="judge r1.json …"` | rationale evaluation — scores the rationales in plan-eval results with rules (code) and an LLM Judge (a model different from the generator) (Judge LLM cost). `check` runs only the rules, `report --md` writes a run log in `docs/rag-eval/`, and `agree` reports agreement between human labels and the Judge (see “Rationale evaluation”) |
| `make rag-compare ARGS="generate a.json --mode llm …"` | RAG vs LLM alone vs LLM + web search — for each plan in plan-eval results, keeps the activity and builds only the evidence part three ways, then compares citation existence (OpenAlex) · source type · claim support (Judge). `import-rag` · `verify` · `report` use no LLM; `generate` · `judge` cost LLM calls (see “RAG vs LLM alone vs web search”) |

On Windows (PowerShell), create it with `py -3.12 -m venv .venv` and install with
`.venv\Scripts\pip install -e ".[dev]"`. Without `make`, run the Makefile commands directly with
`.venv\Scripts\python` (e.g. `-m ruff check .`, `-m pytest`, `-m uvicorn app.main:app --reload`).

## Database (Supabase)

1. Apply the migrations to the Supabase project in name order — `supabase/migrations/20261021000000_init_evidence.sql`,
   `supabase/migrations/20261026000000_evidence_contracts.sql`, `supabase/migrations/20261027000000_care_records.sql`,
   `supabase/migrations/20261029000000_care_rounds_selection.sql`, `supabase/migrations/20261103000000_care_sessions_lang.sql`.
   Paste each into the dashboard SQL Editor or run `psql "$DATABASE_URL" -f <file>`. The second file applies only
   while `papers` · `evidence_records` are empty; if they have rows, it stops without changing anything. The third
   file holds the session record tables and references `auth.users` (Supabase Auth).
2. Copy the connection string from the dashboard's **Connect** dialog into `DATABASE_URL` in `.env`.
3. Create the session checkpoint tables with `make checkpoint-setup`. Run it again after upgrading
   `langgraph-checkpoint-postgres`; migrations already applied are skipped.
4. Load the evidence (`scripts/seed_evidence.py`). The 84 papers in `data/manifest.csv` go into `papers`, and the
   65 rows of the evidence-arm sheet become 61 records × 2 chunks (METHODS · RESULTS) = 122 rows in
   `evidence_records`. First do a dry run to see the counts and the review list (`evidence_review.csv`), then name
   the target DB. Embeddings need `OPENAI_API_KEY`.

   ```bash
   XLSX=../Research_paper/LUMI_R_C_Research_2026-10-22/LUMI_측정도구_프로토콜_2026-10-23.xlsx
   make seed-evidence XLSX=$XLSX ARGS=--dry-run
   make seed-evidence XLSX=$XLSX ARGS="--database-url <connection string>"
   ```

   Name the target DB every time — there is no default, so nothing is written to the shared Supabase in `.env` by
   mistake. Rerunning only updates rows whose (`paper_id`, `experiment_id`, `section`, `chunk_idx`) match, so ids
   are kept. Session records (`care_rounds.evidence_record_ids`) and plans (`CarePlan`) point at these ids, so rows
   are never deleted and reinserted. Values a person confirmed against the original go into
   `data/evidence_overrides.json`, then rerun (`data/README.md`, Korean). The study medium is not in the sheet and
   comes from the `media` of the activity JSON that cites the row — changing only how an activity is delivered
   (PR-10 video mode, #63) would change the study medium on the next load, so the study medium is pinned in
   `evidence_overrides.json`.
   `content` is the paper's original METHODS · RESULTS passage for records with an excerpt in
   `data/evidence_excerpts.json`, and the sheet summary for records without one (papers not obtained, etc.)
   (`--no-excerpts` uses summaries for all). To rebuild the excerpts:
   `make parse-papers ARGS="--with-records"` (Docling; scanned PDFs go straight to RapidOCR; `.[ingestion]` extras;
   the cache is `01_fulltext/` in the private Research_paper repository) → `make extract-excerpts XLSX=$XLSX` (the
   LLM picks, then the passage is aligned to sentences in the body; review list `evidence_excerpts_review.csv`) →
   `make seed-evidence`. Compare before and after with “Evidence retrieval evaluation”.
5. After `make run`, check the connection, the extensions (pgvector · pg_trgm) and the checkpoint tables with
   `GET /health/db`.

| Connection | Port | Network | Used by |
| --- | --- | --- | --- |
| Direct connection | 5432 | IPv6 (without the IPv4 add-on) | long-running servers (VM · container) |
| Shared pooler — session mode | 5432 | IPv4 | local development, long-running servers on IPv4 networks |
| Shared pooler — transaction mode | 6543 | IPv4 | serverless functions |

Transaction mode does not support prepared statements. `app/db.py` and the checkpointer pool turn off psycopg's
automatic prepare, so the same code works on all three. LangGraph's `AsyncPostgresSaver.from_conn_string()`
prepares every query, so it is not used.

| Table | Contents |
| --- | --- |
| `papers` | one paper = one row of `data/manifest.csv`. `id` is the R/C master ID (`rc_id`); the old ID from the 36-paper sheet is kept in `legacy_36_id` (not a join key). `study_design` · `fulltext_status` are the manifest values as is; without a DOI, `url` points to the original |
| `evidence_records` | per-experiment Evidence Records. Source passage (`content`), metadata (`metadata` jsonb), embedding (`vector(1536)`) and the model that made it (`embedding_model`). One row per (`paper_id`, `experiment_id`, `section`, `chunk_idx`) |
| `user_consents` · `care_sessions` · `care_rounds` · `care_texts` | consent and session records (see “Auth · session records”). Rows are deleted along with `auth.users` |
| `langgraph.checkpoints` and 3 more | session checkpoints. Created by `make checkpoint-setup`, which records the applied version in `checkpoint_migrations` |

This project has had the Supabase Data API (`/rest/v1`) turned off from the start. The BE connects to Postgres
directly and the FE uses only Supabase Auth, so it is not needed. As a result the publishable key reaches no table
(a call returns 503 `PGRST002`). So that nothing is exposed even if it is turned on later, the tables in the
`public` schema have RLS enabled with no policies. This does not affect the backend, which connects as the table
owner, the `postgres` role.

The checkpoint tables live in the `langgraph` schema, which stays unexposed even with the Data API on. The library
does not prefix table names with a schema, so the checkpointer pool points at the schema with the connection
option `-c search_path=langgraph`. In transaction mode a `SET` does not survive into the next transaction, but the
Shared pooler (Supavisor) keeps a separate pool per search_path and so preserves this option. Re-check this if you
switch to the Dedicated pooler (PgBouncer).

The DB tests (`tests/agent/test_checkpoint.py` · `tests/retrieval/test_hybrid_db.py` ·
`tests/agent/test_validation_db.py` · `tests/api/test_records_db.py`) run only when `TEST_DATABASE_URL` is set and
are skipped in CI. The checkpointer test checks that a session continues on a pool other than the one that started
it. Evidence search and validation are checked — filters · keywords · RRF and verdicts — inside a transaction that
swaps `evidence_records` for 16 fixture rows (`tests/evidence_db.py`) and is rolled back at the end (the loaded
evidence stays). Session records likewise run a session to the end on a stand-in graph and check the written rows
inside a transaction that is rolled back. The local Postgres puts the `extensions` schema on the search_path as
Supabase does, mimics `auth.users` · `auth.sessions`, and then applies the migrations.

```bash
docker run -d --name lumi-pg -e POSTGRES_PASSWORD=postgres -p 54329:5432 pgvector/pgvector:pg17
psql "postgresql://postgres:postgres@localhost:54329/postgres" -c 'create schema extensions' \
  -c 'alter database postgres set search_path = "$user", public, extensions' \
  -c 'create schema auth' \
  -c 'create table auth.users (id uuid primary key, is_anonymous boolean not null default false, created_at timestamptz default now())' \
  -c 'create table auth.sessions (id uuid primary key, user_id uuid not null references auth.users on delete cascade, created_at timestamptz default now(), updated_at timestamptz default now(), refreshed_at timestamp, not_after timestamptz)' \
  -f supabase/migrations/20261021000000_init_evidence.sql \
  -f supabase/migrations/20261026000000_evidence_contracts.sql \
  -f supabase/migrations/20261027000000_care_records.sql \
  -f supabase/migrations/20261029000000_care_rounds_selection.sql \
  -f supabase/migrations/20261103000000_care_sessions_lang.sql
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/postgres make test
```

### RAGAS metric mapping

RAG is evaluated against two RAGAS retrieval metrics and two generation metrics, measured with this repository's
scripts (the RAGAS library is not used). Factual Correctness compares against a reference answer, but a rationale
has no single correct sentence, so it is not used.

| RAGAS metric | This repository's evaluation | 2026-10-29 |
| --- | --- | --- |
| Context Recall | evidence retrieval evaluation Recall@K — share of gold records in the top K (the same computation as RAGAS ID-based Context Recall) | planner · hybrid Recall@5 0.989 |
| Context Precision | evidence retrieval evaluation CP@5 — the RAGAS Context Precision@5 formula, relevance judged by gold records | planner 1.000 (structural — after scope · filters the candidates are gold only), open hybrid 0.575 · dense 0.772 |
| Faithfulness | rationale evaluation Judge `faithful` · `outcomes` (+ rules `years` · `stats`) — PASS · FAIL per rationale | faithful 36/36 (Judge 35, adjusted by humans) · outcomes 35/36 |
| Response Relevancy | rationale evaluation Judge `personal` — user state → reason for the choice → evidence | 36/36 |

- The Judge numbers were checked against human judgments (18 cases with the Judge's verdict hidden, agreement
  15/18) — see “Rationale evaluation”.

### Evidence retrieval evaluation

`data/rag_eval.jsonl` is the evaluation set for evidence search (`hybrid.search`) (renewed 2026-10-28, 180 cases).
It covers every search situation the production plan node can meet (activity × goal × user state).

Evaluation records go into `docs/rag-eval/`, as for the rationale evaluation, named
`YYYY-MM-DD-retrieval-<topic>.md` — `run --md` (or `record` from a saved result) writes the run log (evaluation
set · evidence DB · search configuration · code commit · metric table · missed gold · evidence from other
activities · fallback top 5), and interpretation and improvement decisions go into the verdict memo
(`-verdict.md`). These records are in Korean.

- **Scenarios.** `data/eval_scenarios.jsonl` is 3 goals × 5 user states (before an event · after an event · after
  prolonged focus · no trigger · not sure) × 2 phrasings (direct · indirect) = 30. The chips (`setting` ·
  `earphones` · `minutes`) are for the plan evaluation, which uses the same scenarios.
- **Queries.** For each scenario, `make rag-eval ARGS="collect"` walks through the candidates for that goal ·
  state (`plan_candidates`, without chips) and gets a `search_query` per activity from the production choose
  prompt — asking again with each chosen activity removed; goal · state are put in directly, without intake. It
  collected 110 situations × 2 phrasings = 220 (gpt-5.4-mini, 67 s, 570k input tokens), and `build` merges
  identical queries for the same situation into 180 cases. The collected queries
  (`data/rag_eval_queries.jsonl`) cannot be regenerated, so they are kept apart from the cases, and `collect`
  fills in only missing (scenario, activity) pairs.
- **Gold.** The records (`paper_id`, `experiment_id`) in the activity node JSON's `evidence[]` that measured the
  goal's primary outcome, attached by `build`. `run` removes from the denominator — and counts separately — gold
  records missing from the DB and gold records whose study condition cannot be cited for the user state
  (`ORDER_COMPATIBILITY`); the latter are records the search's order filter excludes on purpose.
- **Groups.** `planner` (163) is situations with gold records; `fallback` (17) is situations where the activity's
  `evidence[]` has no evidence for the goal outcome, so the same filters search everything (PR-15 · PR-21 S1,
  PR-03 S2). In fallback the top 5 are always other activities' evidence, so instead of scores it looks at the
  number of records in the top 5 (`found@5`) and the list. `run --open` also runs the planner queries without
  filters · scope (embedding-only and keyword-only quality, for RRF-weight experiments).
- **Averaging.** Every case weighs the same — an average over every search situation production can produce. S2
  has 17 activities (S1 · S3 have 4 each), so 119 of the 163 planner cases are S2.
- `make rag-eval ARGS="run --misses"` reports, for three methods — hybrid (the same RRF as production) · dense ·
  keyword — Hit@K · Recall@K · MRR (K = 3 · 5 · 10), CP@5 (RAGAS Context Precision@5 — the precision up to each
  gold position in the top 5, averaged; 1 if every gold record ranks above every non-gold one) and Foreign@5 (the
  share of top-5 records outside the activity's `evidence[]` — lower is better), and `compare` compares two results
  saved with `--out`. K counts records — in the order records first appear in the chunk ranking (the same as
  production plan carrying 5 distinct records, `hybrid.top_records`). Up to the 2026-10-28 measurement it counted
  chunk positions. Run it on lumi-pg — the production Supabase session pooler (5432) has a 15-client limit, so the
  evaluation pool cannot open connections when other connections overlap.
- `tests/retrieval/test_rag_eval.py` checks that the committed cases equal the `build` output and that every
  situation that can be a candidate has a collected query. If it breaks because activity JSON changed, rerun
  `collect` → `build`.

2026-10-29 measurement (180 cases, text-embedding-3-small, lumi-pg). Records (Korean):
[verdict memo](docs/rag-eval/2026-10-29-retrieval-verdict.md) ·
[baseline](docs/rag-eval/2026-10-29-retrieval-baseline.md) ·
[record top 5](docs/rag-eval/2026-10-29-retrieval-top-records.md) ·
[label fixes](docs/rag-eval/2026-10-29-retrieval-labels.md) ·
[Context Precision@5](docs/rag-eval/2026-10-29-retrieval-context-precision.md):

| view · method | n | Hit@5 | Recall@3 | Recall@5 | Recall@10 | MRR | CP@5 | Foreign@5 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| planner · hybrid | 163 | 1.000 | 0.937 | 0.989 | 1.000 | 1.000 | 1.000 | 0 |
| planner · dense | 163 | 1.000 | 0.937 | 0.989 | 1.000 | 1.000 | 1.000 | 0 |
| planner · keyword | 163 | 0.957 | 0.855 | 0.895 | 0.895 | 0.957 | 0.957 | 0 |
| open · hybrid | 163 | 0.712 | 0.456 | 0.566 | 0.756 | 0.630 | 0.575 | 0.639 |
| open · dense | 163 | 0.902 | 0.616 | 0.758 | 0.901 | 0.819 | 0.772 | 0.534 |

| view · method | n | mean found@5 | no evidence |
| --- | --- | --- | --- |
| fallback · hybrid | 17 | 2.77 | 2 (12%) |

- Three things changed from 2026-10-28 (planner · hybrid Recall@5 0.920 · 34 cases missing gold). ① The top 5
  counts records, not chunks — one experiment's METHODS · RESULTS used to take two slots, pushing the S3 PR-11 ·
  PR-01 gold (P07-E1 · P03-E1 · P01 · P04) to ranks 6–8; all 13 such cases now make the top 5 (0.942). ② Affect
  labels where activity JSON and records disagreed were aligned with the originals — E12-E1 (no effect on state
  mood) · E15 (positive affect) · E21 (affect g = 0.51) were fixed through `data/evidence_overrides.json`, so
  PR-18 · PR-19 find their own evidence (0.973). ③ E11-E1 measured only happiness · depression, so the affect tag
  was removed from PR-19's JSON — this fixed the gold definition, not the search (0.989).
- The remaining 6 cases are all PR-14. It has 7 gold records, which cannot all fit in a top 5 (Recall@10 1.000),
  and E02-E1 · P30-E1, which have no original text, rank 6–7.
- The planner's CP@5 of 1.000 comes from structure, not ranking — none of the 163 cases has a non-gold record in its
  top 5 (the search runs inside the activity's `evidence[]` with goal-outcome · study-condition filters, so the
  candidates are gold only). Ranking quality itself shows in open, without scope · filters, where dense (0.772)
  beats hybrid (0.575) (verdict memo, verdict 6).
- 19 gold records were removed from the denominator by the user-state policy (e.g. a post-stress recovery study for
  a user before an event).
- Two fallback cases (PR-15 chosen before an event · after prolonged focus) have an empty top 5, so the Reference
  card has no evidence — the S1 coloring study there is to borrow, P32-E1, is a post-stress (POST_STRESS)
  condition and cannot be cited for those states. PR-21 borrows PR-09's N26-E1 · P13 · P14 (and PR-01's P24-E1 ·
  P33-E1), and PR-03 borrows PR-01's P24-E1 · P33-E1 · N24.
- In open, without filters, dense (Recall@5 0.758) is still better than hybrid (0.566) — the basis for the
  RRF-weight experiment. Production searches within scope · filters, so this gap does not show in the planner
  results (hybrid and dense are equal there).

The two measurements below used the old evaluation set (51 cases — `planner` 9 · `template` 21 · `open` 21,
queries from 2026-10-26). `template` was research-term queries per protocol × goal, and `open` was the same
queries run without filters.

2026-10-27 measurement (text-embedding-3-small, lumi-pg). 'Summary' is when `content` was the 122
evidence-arm sheet summary chunks; 'original' is after 58 records (116 chunks) were replaced with original
METHODS · RESULTS excerpts (`data/evidence_excerpts.json`; the scanned P05 · P26 were checked against page images).

| group · method | n | Hit@5 summary → original | Recall@5 summary → original | MRR summary → original |
| --- | --- | --- | --- | --- |
| planner · hybrid | 9 | 1.000 → 1.000 | 0.867 → 0.911 | 1.000 → 1.000 |
| template · hybrid | 21 | 1.000 → 1.000 | 0.875 → 0.882 | 0.863 → 0.843 |
| open · hybrid | 21 | 0.714 → 0.714 | 0.558 → 0.517 | 0.648 → 0.541 |
| open · dense | 21 | 0.714 → 0.762 | 0.558 → 0.638 | 0.648 → 0.690 |
| planner · keyword | 9 | 0 → 1.000 | 0 → 0.933 | 0 → 1.000 |

- With summaries, keyword scored 0 in all three groups — English query words never matched the Korean summaries,
  so hybrid was effectively dense. Switching to the originals revived keyword, and the filtered paths the planner
  actually uses (planner · template) stayed the same or improved.
- In `open`, without filters, dense went up and hybrid went down. The still-weak keyword ranking blurs the dense
  ranking in RRF — tuning the RRF weights (or the number of keyword candidates) is the next experiment.
- On the same day `make plan-trial ARGS="S1 S2 S3"` chose S1 → PR-09, S2 → PR-06, S3 → PR-01 with both summaries
  and originals. Input tokens per plan grew from 3.9–5.6k to 4.9–6.4k (longer excerpts in the rationale prompt).

**Activity evidence scope (2026-10-28).** plan first searches within the activity's `evidence[]` (records the
activity node owner chose after reading the originals), and searches everything with the same filters only when
no record fits the goal outcome · user state or `evidence[]` is empty (PR-21) (`nodes/planner.find_evidence`; the
SQL matches `coalesce(experiment_id, paper_id)` against the keys of `evidence[]`). Several activities share a
medium (nature images 4 · meditation 3 · breathing · nature sounds · video · walking · coloring · writing 2 each),
so with a medium filter alone, other activities' evidence got mixed into the Reference card. That is why
`evidence[].evidence_record_ids` is not back-filled and stays empty — evidence is found by key at run time, so the
ids need not be written down.

| group · hybrid | n | Foreign@5 before → after | Hit@3 before → after | Recall@3 before → after | Recall@5 before → after | MRR before → after |
| --- | --- | --- | --- | --- | --- | --- |
| planner | 9 | 0 → 0 | 1.000 → 1.000 | 0.652 → 0.652 | 0.911 → 0.911 | 1.000 → 1.000 |
| template | 21 | 0.234 → 0 | 0.952 → 1.000 | 0.705 → 0.816 | 0.882 → 0.906 | 0.843 → 1.000 |
| open | 21 | 0.498 → 0.498 | 0.667 → 0.667 | 0.447 → 0.447 | 0.517 → 0.517 | 0.541 → 0.541 |

- All 8 template cases that mixed in other activities' evidence (PR-04 · 10 · 11 · 12 · 18 · 19 — e.g. PR-19 took
  PR-18's E14-E1 · E13 instead of its own E11-E1 · E12-E1) disappeared. planner (the demo S1 · S2 · S3 queries) was
  0 to begin with and stays so. On the same day `make plan-trial ARGS="S1 S2 S3"` chose PR-09 · PR-06 · PR-01, and
  those queries had the same top 5 with or without the scope.
- template's Recall · MRR going up is somewhat expected: the gold is the activity's `evidence[]`, so narrowing the
  scope helps. The metric to watch is Foreign@5. `open` has neither scope nor filters and is unchanged — the
  RRF-weight experiment happens there.
- Gold records missed even within the scope are a data problem, not a search problem. The activity JSON says E11-E1 ·
  E12-E1 · E15 measured AFFECT, but their record metadata has only OTHER, so they never pass the S2 outcome filter
  (fixed against the originals on 2026-10-29 — the measurement above). E02-E1 · P30-E1 have no original text, only
  sheet summaries, so they rank low.

### Plan evaluation

`make plan-eval` runs the production plan node with a real model for each of the same 30 scenarios
(`data/eval_scenarios.jsonl`) and judges it by rules (LLM cost, evidence DB read-only — run it on lumi-pg). Goal ·
user state · chips (place · earphones · minutes) are put in directly as the intake result (intake is evaluated
separately), and the 6 `change_activity` scenarios run one more round after deciding to change the first plan's
activity. The answer is not one activity but rules — a plan comes out · the LLM's first choice passes without a
re-ask · the chosen activity is among the expected candidates (`plan_candidates`) · the relaxed conditions match
expectations · the final plan passes `check_plan` · a different activity is chosen after an activity change. The
amount of Reference card evidence and evidence borrowed from other activities are counted separately (a card
without evidence is a lack of evidence, not a rule violation).

2026-10-28 measurement (gpt-5.4-mini, lumi-pg) — 36 plans (30 scenarios + 6 activity changes):

| Rule | Pass |
| --- | --- |
| A plan comes out | 36/36 |
| The first choice passes without a re-ask | 36/36 |
| Within the expected candidates · relaxed conditions as expected · passes `check_plan` | 36/36 |
| Chooses a different activity after an activity change | 6/6 |
| The Reference card has evidence | 35/36 |

- The only card without evidence is S1-pre-b (before an event · office · no earphones · 5 minutes → PR-15 is the
  only candidate). The cause is the same as the zero-evidence fallback cases in the retrieval evaluation — the
  coloring study there is to borrow, P32-E1, is a post-stress condition. The other two cards that chose PR-15
  borrowed PR-14's P32-E1.
- Activities chosen in first plans: S1 PR-09 8 · PR-15 2, S2 PR-06 7 · PR-01 2 · PR-19 1, S3 PR-01 6 · PR-11 3 ·
  PR-10 1. Tight chips shift the choice to the remaining candidates (e.g. before a presentation · office · no
  earphones · 5 minutes → PR-19 writing).
- One plan took 3.1 s on average, with 4,091/202 tokens (two calls: choice · explanation).

Since 2026-10-29 the candidate order is a grade-weighted draw, so the evaluation also runs like production, with
the session ID `<--seed>:<scenario>` per scenario (default `plan-eval`). The summary also reports how many distinct
activities were chosen in first plans and how often the LLM chose the front of the recommendation order. The
measurement above is from the grade-order days (reproduce it with `--seed ''`). Under grade order, only 11 of the 20
activities could rank first across the 420 input combinations (goal × user state × time chip × place ·
earphones), and on the default path with an unknown place the choice was fixed per goal at PR-09 · PR-06 · PR-01 —
with the weighted draw it is 19 (PR-12 is 30 minutes, so it is not a candidate even with the 20-minute chip).

2026-10-29 measurement (gpt-5.4-mini, lumi-pg, `--seed plan-eval`) — 36 plans:

| | Grade order (10-28) | Weighted draw · all candidates | Weighted draw · rank + first 5 |
| --- | --- | --- | --- |
| 6 rules | all pass | all pass | all pass |
| Activities chosen in first plans | 7 kinds | 10 kinds | 13 kinds |
| Chose the front of the recommendation order | — | 27/36 | 35/36 |
| The Reference card has evidence | 35/36 | 34/36 | 34/36 |

- Shown every candidate and told only to "pick the front one", the model went down to PR-06 · PR-01 ('only 5
  minutes, so less of a burden') — as far as the 9th — and wrote 'the front one' even after choosing the 6th. With
  ranks on the candidates, the skip reasons pinned to four (constraint · place · an activity the user is reluctant
  about · the previous rejection reason) and only the first 5 shown, 35/36 chose the front one (the remaining one
  is S3-daily-b, which skipped outdoor · nature-sound activities).
- Activities chosen in first plans: S1 PR-09 5 · PR-15 4 · PR-21 1, S2 PR-01 3 · PR-19 2 · PR-06 · PR-07 ·
  PR-08 · PR-14 · PR-18 1 each, S3 PR-01 6 · PR-11 2 · PR-04 · PR-10 1 each. S1 · S3 have few candidates (2 at 10
  minutes), so choices still gather on two or three activities — a supply problem.
- One more card has no evidence (S1-fatigue-b PR-15) — activities with weak evidence now also come first. The card
  states that no evidence was found.

### Rationale evaluation

`make plan-eval ARGS="… --out r1.json"` saves, per plan, the rationale and the input its explanation call saw (the
user message verbatim · evidence · composition · goal-outcome grades · card badges). `make rationale-eval` scores
this in two layers. Model output wobbles even for the same input (on 10-28, flag names leaked in only 4 of 6 S1
runs), so results from several plan-eval runs go in together, counted as one case per plan.

Evaluation records are kept by date in `docs/rag-eval/` — the run log that
`rationale_eval.py report <judged.json> --md docs/rag-eval/YYYY-MM-DD-<topic>.md` makes (setup · rules · Judge
distribution · violations) and the verdict memo a person writes (`-verdict.md` — background · how to read ·
verdicts · limitations). 2026-10-29 (Korean): [verdict memo](docs/rag-eval/2026-10-29-rationale-judge-verdict.md)
· [baseline](docs/rag-eval/2026-10-29-rationale-baseline.md) · [after the fix](docs/rag-eval/2026-10-29-rationale-must-disclose.md)
· [after the retrieval improvement](docs/rag-eval/2026-10-29-rationale-after-retrieval.md).

| Layer | Items | What is judged |
| --- | --- | --- |
| Rules (code, `check`) | sentences · script · codes · stats · factors · experimental · years · no_evidence | the explanation prompt's instructions that can be judged from the characters — 2–3 sentences (4 if there is a '다만' ("however") sentence disclosing a limitation), only Hangul · Latin letters (an output error where other scripts creep in), no enums · flags · IDs verbatim, no statistics, no names of the chosen content Factors, '실험적' ("experimental") only when the composition is EXPERIMENTAL · the goal-outcome grades are all D · the card has the experimental badge, cited years within the evidence list, and saying that no evidence was found when there is none |
| Judge (LLM, `judge`) | faithful · outcomes · hedged · disclosure · personal | what needs reading for meaning — no studies · results that are not in the evidence · excerpts, improvement claimed only for outcomes listed as improved (affect results not turned into stress reduction), no definite effects and no stretching to clinical claims, flags · lack of evidence disclosed in plain words, user state → reason for the choice → evidence |

- The rules are in `app/agent/rationale_rules.py`, and the production plan node post-checks rationales with the same
  rules — on a violation it asks for the explanation once more with the reason (sentence count is a presentation
  issue, so it is not re-asked), and if it still violates, the second one is used. 「추천 이유를 다시 씀」
  (rationale rewritten) in the plan-eval summary is that count.
- English sessions (`RationaleFacts.lang`) check the same rules against English sentences — Latin letters only,
  the limit sentence starts with 'That said' · 'However', 'experimental' instead of '실험적', English wording for
  "no evidence" (`_NO_EVIDENCE_EN`), and the chosen content found by English terms set per value
  (`FACTOR_TERMS_EN`).
- The '개선 지표' (improved outcomes) on each evidence line is written as what that evidence actually measured, not
  the outcome category name (`prompts.outcome_display`, split by `results[].measure`). Anxiety · subjective stress
  share one category (`SUBJECTIVE_STRESS`), so writing the category name attached '불안' (anxiety) even to
  Toussaint 2021, which measured only a relaxation scale, and the rationale disagreed with the abstract (2026-10-30
  RAG comparison) — now it writes what was measured, such as '이완 상태' (relaxation state) · '불안' (anxiety) ·
  '주관적 스트레스' (subjective stress), and falls back to the category name when that is unknown.
- The Judge is a model different from the generator (`LLM_MODEL`, gpt-5.4-mini) — gpt-5.5 by default, reasoning
  medium — since a model is lenient on its own writing. Its input is the user message the explanation call saw,
  verbatim (only the content Factor block is removed), and per item it returns PASS · FAIL (NA only for
  disclosure) with the problem phrase · reason. It fails an item only for a clear violation.
- Judge numbers are quoted only after a person checks them. `sheet` exports every Judge FAIL plus a PASS sample per
  item to CSV; once a person fills in the human column (PASS · FAIL · NA, or O · X · -), `agree` reports the
  agreement. Only items with confirmed agreement are quoted as numbers.

2026-10-29 baseline (generator gpt-5.4-mini · Judge gpt-5.5 medium · lumi-pg · the candidate order is the same
weighted draw as production, `--seed plan-eval`) — 36 plans (14 kinds of activities chosen in first plans):

| Layer | Result |
| --- | --- |
| Rules | sentences · script · codes · stats · experimental · years 36/36, no_evidence 2/2, **factors 34/36** |
| Judge | personal · hedged 36/36, faithful 34/36, outcomes 31/36, **disclosure 3/16** |

- disclosure — of the 16 cases with evidence flagged for opposite results (CONFLICTING_EVIDENCE) · adapted stimulus
  (ADAPTED_STIMULUS), only 3 disclosed it in the text. The Judge counted 7/22 — it graded 4 cases with only
  non-supporting evidence (NOT_SUPPORTING) as PASS · FAIL instead of NA. This was corrected by matching the
  evidence flags against the sentences.
- outcomes · faithful — calling results outside the improved outcomes effects (electrodermal activity · state
  rumination), presenting evidence that does not support the goal as an effect (PR-19 · PR-20), and turning affect
  results into stress · anxiety improvement (Chen 2025).
- factors — the chosen scenery · artwork subject appeared in the text ("숲·호수·바다·산을 섞어", mixing forest ·
  lake · sea · mountain; "물 풍경 작품을 고른 것은", choosing a waterscape artwork).
- Mismatch with the card — PR-06 · PR-07 cards (4/36) get the 'content that changed the research stimulus' badge
  (protocol `user_facing_flags.when_adapted_script`), but the explanation input lacked this information, so the
  rationale could not disclose it.
- The same day it was also measured under grade order (before the weighted draw — same as `--seed ''`) (108 for
  the rules · 72 for the Judge). Output with another script in the middle of a Korean sentence occurred 3/108
  (Devanagari 'अभ्यास' · Arabic 'فاص' · author Mohd → 'محمد' — about a 13% chance of at least once in 5 plans),
  ID exposure 1/108 (PR-06), factors 106/108, disclosure 3/30, faithful 66/72, outcomes 63/72. The big outcomes
  misses were 'relaxation' outside the improved outcomes (Toussaint 2021) ×6 and saying Hicks 2020, which does not
  support the goal, showed "fewer errors" (the excerpt says omission errors increased after exposure).
- After reading the first scoring (36), two Judge criteria were fixed — the '개선 지표' (improved outcomes) field
  also counts as support (some evidence excerpts are from the methods part), and the '이미지 제공' (image delivery)
  line of the user state alone does not require a disclosure (activities that use no images, such as breathing ·
  relaxation). The Judge cost 87k/25k tokens for 36.

2026-10-29 after the fix (same conditions — weighted draw `--seed plan-eval` · 36 plans · Judge gpt-5.5). The plan
node post-checks the rationale with the character rules (re-asking once with the reason on a violation), code
decides the limitations that must be disclosed and puts them into the explanation input
(`prompts.must_disclose` — the same judgment as the card badges), and when supporting evidence exists,
non-supporting evidence is removed from the explanation input:

| | Baseline | After the fix | After the retrieval improvement |
| --- | --- | --- | --- |
| Rules — factors | 34/36 | 36/36 | 36/36 |
| Rules — the rest (sentence count · other scripts · IDs · statistics · years · '실험적') | 36/36 | 36/36 | 36/36 |
| Judge — faithful · outcomes | 34 · 31 | 34 · 34 | 35 · 35 |
| disclosure (evidence flags matched against sentences) | 3/16 | 15/16 | 18/18 |
| Agrees with the card's adapted-stimulus badge (PR-06 · PR-07) | 0/4 | 4/4 | 5/5 |
| All pass | 19/36 | 33/36 | 33/36 |
| Rationale rewritten (post-check re-ask) | — | 0/36 | 0/36 |

- The sentence count became 2–3, or up to 4 with a '다만 …' ("However, …") sentence disclosing a limitation — with
  the must-disclose items added, 7 of 36 had 4 sentences (state → reason → evidence → limitation).
- The first fix carried the must-disclose items in plain declarative form with parentheses, and they were pasted in
  as is — "…좋아졌습니다. 제공 콘텐츠가 연구에서 쓴 자극과 다르다(LUMI에 맞게 바꿨다)" ("…improved. The
  delivered content differs from the stimulus used in the study (adapted for LUMI)") (no Judge item covers style,
  so the numbers did not catch it). They were rewritten as polite 합니다체 sentences and gathered into one
  '다만 …' sentence.
- Remaining failures — citing Chen 2025, which has only affect results, as evidence of anxiety · stress improvement
  (2), and not disclosing an adapted stimulus (1). No post-check re-ask happened in these 36 (other scripts creeping
  in is rare, about 3 in 108 — the behavior is covered by tests).
- After the retrieval improvement (record top 5 · affect labels · PR-10 study medium pinned — see “Evidence
  retrieval evaluation”) — more cited evidence raised input tokens per plan from 7,043 to 7,541 (+7%), and every
  plan disclosed, including the PR-18 · PR-19 plans newly flagged with opposite results. The remaining failures are
  one each: '건강한 성인' (healthy adults), which is not in the evidence (copied from the explanation prompt into
  the population description) · '마음챙김 점수' (mindfulness score), outside the improved outcomes · a definite claim
  of an immediate effect.
- Human labels (18 cases `sheet` drew from the after-retrieval-improvement run, judged with the Judge's verdicts
  hidden) — 15/18 agree with the Judge. The 3 disagreements were the 3 'remaining failures' just above; on
  adjudication the Judge was right on '마음챙김 점수' (outcomes) · the definite immediate effect (hedged), and
  '건강한 성인' (faithful) was ruled a pass — calling evidence without a clinical-sample flag a general adult sample
  is not an invented condition (added to the Judge criteria). With the adjudicated labels, faithful is 36/36 · all
  pass 34/36. The blind human judgments were all passes, so κ is 0 — this sample cannot tell whether the two find
  violations in the same places ([verdict memo](docs/rag-eval/2026-10-29-rationale-judge-verdict.md), verdict 5).

### RAG vs LLM alone vs web search

This comparison answers "Couldn't you just ask ChatGPT or search the web for the evidence?" In LUMI, RAG's job is
presenting the evidence in the rationale, so for the 36 plans of the plan evaluation the activity stays fixed as
LUMI chose it, only the evidence part is built three ways, and all are measured with the same yardstick
(`make rag-compare`, `scripts/rag_compare.py`). All three use the production generator, gpt-5.4-mini.

- **A. LUMI RAG** — the plan-eval rationales as they are (evidence = validated records). **B. LLM alone** — the
  same input minus the evidence · must-disclose · content Factor blocks, with the model citing studies it knows
  (the same input 3 times). **C. LLM + web search** — the same input with OpenAI's `web_search` tool. **D. Plain
  search** — 6 scenario sentences typed into Google, with the source types on the first page counted by hand.
- **Citations** are the 'Author (year)' strings code finds in the rationale; **existence** is decided as FOUND ·
  MISMATCH (bibliographic error) · NOT_FOUND by looking up OpenAlex (DOI → title) → Crossref → author · year. The
  **Judge** (gpt-5.5) decides per citation whether the claim is supported by the abstract and whether the study
  condition (before · after an event · population · medium) fits the user state, and per rationale hedged ·
  personal — A, too, is judged against the same abstracts, not the excerpts production sees. Judge numbers are
  used after matching human labels with `sheet` · `agree`.

2026-10-30 measurement — records (Korean): [verdict memo](docs/rag-eval/2026-10-30-rag-compare-verdict.md) ·
[run log](docs/rag-eval/2026-10-30-rag-compare.md) · [Google results](docs/rag-eval/2026-10-30-rag-compare-google.md).
Cells with an arrow (→) are the morning measurement → A measured again the same day after fixing the P18 labels
(#131) · outcome display names (below, #133) · N24 measure (#135)
([run log after the fix](docs/rag-eval/2026-10-30-rag-compare-after-fix.md)). B · C are the morning measurement as
is; only for the one plan where the new A chose a different activity (S2-fatigue-a) were B · C regenerated with
that activity:

| | A. LUMI RAG | B. LLM alone | C. LLM + web search |
| --- | ---: | ---: | ---: |
| Citation existence — match · bibliographic error · not found | **70 · 0 · 0 → 68 · 0 · 0** (100%) | 36 · 9 · **10** → 37 · 9 · **10** (66 · 16 · 18%) | 53 · 9 · 0 (85 · 15 · 0%) |
| Jaccard of citation sets across 3 runs of the same input | — | **0.10 → 0.09** | — |
| Claims supported, among citations with source text | 52/63 (83%) → **58/61 (95%)** | 13/28 (46%) | 55/61 (90%) |
| Study condition mismatched with the user state | **1/70 → 3/68** | 7/55 → 7/56 (12%) | **14/62 (23%)** |
| Research-claim sentences without a source / 36 rationales | 1 | 3 | 11 |
| hedged · personal | 36/36 | 36/36 | 36/36 |
| Cost · time / case | $0.0068 (choice + explanation) · 3.6 → 3.4 s | $0.0015 · 3.7 s | $0.0153 → $0.0156 · 5.5 → 5.6 s |

- In B a third of the citations are wrong (18% are papers that do not exist — plausible ones such as "Kang et al.
  (2014) … A meta-analysis"), and it cites different papers every time for the same input. C's citations exist
  (PubMed 59/62), but it cites studies of exposure **before** a stress task for users **after** stress, and
  clinical patient samples · 2–4-week interventions · video studies as evidence for a 5-minute photo activity —
  exactly what A filters out in code (`ORDER_COMPATIBILITY` · `POPULATION_MISMATCH` · medium).
- In the morning measurement, all 5 of A's claim mismatches were Toussaint 2021 (whose abstract mentions only
  'relaxation state'). First the record's labels were checked against the original (Europe PMC full text): the
  unmeasured affect · mood (AFFECT) was removed and the EDA · heart-rate results were written as in the original
  (`data/evidence_overrides.json` P18-E1 · E2 · E3; the affect · mood grade of PR-07 · PR-08 B → —, #131), but
  **fixing the labels alone left all 5 mismatches** — the rationale merely changed to "anxiety · subjective stress
  improved", which is the category name of the `SUBJECTIVE_STRESS` outcome. After the '개선 지표' (improved
  outcomes) on each evidence line was changed to what the evidence measured (`results[].measure` → '이완 상태'
  (relaxation state)) instead of the category name (`prompts.outcome_display`, see “Rationale evaluation”), all 5
  Toussaint claims became supported, and A's claim support matched C's (54/60 vs 55/61). Of the 6 unsupported
  claims left at that point, 3 were on Fan & Baharum 2024 (N24), whose empty measure let the category name through,
  so N24's measure was set to '스트레스' (stress) (#135) and only the 3 plans citing N24 were regenerated · judged —
  all 3 became supported: 58/61 (95%), 0 mismatches. The remaining 3 unsupported claims are all Balban 2023's
  'anxiety' (the STAI result in the body is not in the abstract — a limitation of judging by abstracts). Condition
  mismatches going 1 → 3 add one case where borrowed evidence (PR-15's P32 Drew 2018, a stress-induction study) was
  cited for a user with no trigger, and one where the Judge this time called the same record a mismatch (Van Hedger
  2019, UNKNOWN in the morning) — from a single run, so this is within judgment noise.
- D (57 Google results): academic 0 · public · medical institutions 3 · press 9 · blogs · communities 29 ·
  commercial 16 — queries about bodily symptoms drift toward arrhythmia · stroke. Naver could not be run because the
  Chrome extension blocked the site. Search quality itself is in “Evidence retrieval evaluation”: keyword vs vector
  (Recall@5 0.895 vs 0.989).

## Deployment (Vercel)

The FastAPI app (`app` in `app/main.py`) runs as a single function on the Vercel Python runtime. With the
repository connected to the Vercel project, a merge into `main` deploys to production. PR branches are not deployed
(see “Deploy branch” below).

| Setting | Value | Why |
| --- | --- | --- |
| Region | `icn1` (Seoul) · `vercel.json` | It must run next to Supabase (`ap-northeast-2`) so queries do not cross the Pacific. The default is `iad1`, and Hobby allows one region |
| Deploy branch | `main` only · `vercel.json` `git.deploymentEnabled` | Hobby creates at most 100 deployments a day (24 hours). Building a Preview on every PR push fills the quota and blocks even `main` deployments (2026-10-29) |
| Python | 3.12 · `.python-version` | the same version as CI |
| `DATABASE_URL` | Transaction pooler (port 6543) | because it is a serverless function (connection table above). It differs from the Session pooler address in local `.env` only in the port |
| `OPENAI_API_KEY` · `ANTHROPIC_API_KEY` | same values as `.env` | the fallback key may be left empty |
| `PIXABAY_API_KEY` · `FREESOUND_API_KEY` · `AI_HORDE_API_KEY` | same values as `.env` | content providers for owner A. Blank providers are not called |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | login token verification (JWKS). If empty, the session API returns 503 |
| `CRON_SECRET` | a long random string | sent as `Authorization: Bearer` when Vercel Cron calls the cleanup job (`vercel.json` `crons`, daily at 18:00 UTC = 03:00 KST). Hobby allows once a day at most |

Environment variables go into the Vercel project's Settings → Environment Variables, and a change takes effect only
after redeploying. After a deployment, a 200 from `https://<deployment URL>/health/db` means the DB connection ·
extensions · checkpoint tables are ready. ship does this check automatically after merging (`automation/pipeline.md`
Step 3, Korean). The Hobby plan cuts function execution at 300 seconds and deploys only commits authored by the
project owner, so teammates' commits may not deploy. In that case, Redeploy from the Vercel dashboard.

## Branch and PR rules

Per-role files and this week's order of work are in [TEAM.md](TEAM.md) (Korean).

Branches go feature → `main`. The infrastructure is a single Supabase, so a separate `dev` would use the same DB and
would not split environments. `dev` will be added once there is a staging environment (a separate Supabase project,
etc.) and a deployment pipeline.

1. Create an issue from a template (기능 구현 feature · 기능 수정 change · 오류 수정 bug fix · 리팩토링 refactoring ·
   문서 작업 docs · 기타 수정 other).
2. Branch `<type>/#<issue number>` off `main`. E.g. `feat/#12`, `chore/#13`
3. Open a PR into `main`. The title is `[type/#issue number] description`, and the body says `close #issue number`.
   It can merge only after CI (`check`: ruff · pytest, `gitleaks`) passes, and it is squash-merged.

`main` is protected: PRs required (0 approvals), required checks `check` · `gitleaks`, no force pushes or deletion.

The **ship harness** automates steps 1–3. After finishing a code change, call `/ship` in Claude Code or `$ship` (or
"ship" · "배포해줘", "deploy it") in Codex, and it runs validation (`make lint` · `make test`) → issue and branch
creation → PR → CI gate → squash merge → main CI check → Vercel production deployment and `/health/db` check. The
source procedure is the harness-neutral spec `automation/ship.md` · `automation/pipeline.md` (Korean);
`.claude/commands/ship.md` and `.agents/skills/ship/SKILL.md` are the entry adapters for each harness. The polling
and gate scripts (`automation/bin/`) are Python using only the standard library, so no bash · jq is needed, and
`tests/automation/` regression-tests them under `make test`. Setup for a new PC (tool install and login, Codex
config, Windows) is in [`automation/README.md`](automation/README.md) (Korean).

The team shares the Supabase DB, so only migrations merged into `main` are applied. Schema changes at the PR stage
are checked first on a local Postgres (e.g. the `pgvector/pgvector` Docker image).

## Not decided yet

- The image-generation provider and key. The chat · extraction model is gpt-5.4-mini alone (fallback
  claude-haiku-5-5), and embeddings use text-embedding-3-small.
- The weights of the candidate recommendation order. By the best grade on the goal's primary outcome A 4 · B 3 ·
  C 2 · otherwise 1, and × 0.3 for activities this user did in the last 5 rounds (`protocols.GRADE_WEIGHT` ·
  `RECENT_FACTOR` · `RECENT_ROUNDS`). Analysis corrects for the randomness with the round records' `selection`
  (order · weights · probability of being first · chosen position), and the weights will be set again once
  per-activity Δ accumulates.
- The Replanning criteria. For now these are provisional rules (not finished → change activity, improved → end, no
  criterion → end, otherwise → change activity, at most 3 rounds). Options: only end after 3 rounds; no change
  activity when there is no other activity; no additional session (the same activity once more) for an activity
  stopped midway; and end is recommended when the step to recommend is not among the options
  (`replanner.next_step_options`). The LLM takes this rule suggestion as its default and changes it only when the
  stop reason · emotion words · first input carry a different signal — compared through the round records'
  `rule_recommended` · `recommended_by_llm`.
- The momentary fatigue self-report scale for S3. None of the 13 papers has a validated instrument, so the
  provisional `INS-MFVAS` (a 3-item mental-fatigue VAS, written by the team, unvalidated) is measured at pre and
  every round, judged by whether fatigue fell by 10 points or more (the threshold is provisional too). Attention
  capacity is not measured (the evidence's S3 primary outcome ATTENTION is task-based) — after checking item
  reliability and the threshold on user data, we will decide whether to switch to an external scale such as ROS.
- STAI-6 · BSRI use Korean translation drafts kept as `PROVISIONAL` for internal testing. Both scales are measured
  only at PRE/FINAL and are not interpreted as validated Korean versions. English sessions use the original items
  (`data/instruments/en/` — BSRI exactly as in the authors' appendix, STAI-6 the original STAI items), so they are
  `READY`. STAI is copyrighted by Mind Garden, so the license must be checked for both languages before a public
  service.
- The intake environment assumptions. In an office · café with earphones, sound and quiet-place activities count
  as possible; a subway · bus is not treated as quiet even with earphones (one place:
  `app/schemas/care.environment_for`). If the place stays unknown, a quiet indoor place is assumed and `setting`
  is left in `missing_slots` — the FE must show "소리가 나와요" ("Sound will play") together with "지금은 어려워요"
  ("Not right now", change activity) on the activity start screen.
- The intake safety judgment is the single `safety` field of ① situation reading (LLM). The first input and every
  free-text answer (re-ask · timing question) pass through ① again, so all of them are judged. A regex word list is
  not used — expressions vary too much and it misses a lot. A hit ends the session (`ended = SAFETY`) and the FE
  shows help information. The LLM's judgment must be checked with real sentences in `make plan-trial`.
- The intake entry: the first question "오늘 상황이 어떠세요?" ("How are things today?") is fixed on the FE start
  screen. If the answer is unrelated (menus · code · random characters), a re-ask fitted to what the user said
  (`slot = restate`, no options, `skip_label` '그냥 시작할게요', "Just start") repeats until a relevant answer
  comes. The 3-question budget counts from after the relevant answer. An unrelated free-text answer to the timing
  question is treated as no cue and the flow moves on (no re-ask).
- The order of relaxing conditions when candidates run short. For now user state → environment, and time is never
  relaxed (`protocols.RELAX_STEPS`). plan · the intake environment-question decision · replan options all use the
  same lookup (`protocols.plan_candidates`). When the environment is relaxed, the FE must show notices such as
  "소리가 나와요" ("Sound will play") from the plan's `environment_gaps` (AUDIO · SCREEN · QUIET · OUTDOORS). With
  the 2026-10-27 protocols, the only case that actually relaxes is S3 + before an event (user state).
- The consent wording (FE) and its review. Today members' session records (including measurements) are treated as
  sensitive information under a required consent, and raw-text retention is an optional consent. Raw text is kept
  for 90 days (`TEXT_RETENTION_DAYS`). Guests get only a notice without consent, so before opening as a real
  service, legal review is needed on whether a notice is enough for the processing during a session (measurements
  included).
- Re-authentication before account deletion. Today there is only the FE confirmation screen, so a stolen token can
  delete the account. Whether to require another social login will be decided with the FE.
- Guest abuse. Guests also call the LLM in every session, so automated anonymous sign-ins could drive up cost.
  Today there is only Supabase's anonymous sign-in rate limit; whether to put a CAPTCHA (Turnstile · hCaptcha) on
  anonymous sign-in is still to be decided.

## Directory

```text
app/
├─ main.py              FastAPI app (with DATABASE_URL: DB pool · Postgres checkpointer · record store · session graph · token verifier · CORS)
├─ config.py            .env settings (DB · LLM/embedding · owner A's content provider keys and timeouts · Supabase · retention periods)
├─ db.py                Supabase Postgres connection pool (registers pgvector types)
├─ auth.py              login token verification — Supabase JWKS (ES256) · login session check (auth.sessions) · current_user
├─ records.py           session records — consent · session · round · raw-text rows and stores (Postgres · memory) · cleanup job
├─ api/routes.py        HTTP endpoints (/health, /health/db)
├─ api/sessions.py      session API — auth · ownership check · interrupt resume · step · SSE/JSON responses · records
├─ api/me.py            records consent (GET · PUT /me/consent) · activity history (GET /me/sessions) · account deletion (DELETE /me)
├─ api/internal.py      cleanup job (GET /internal/cleanup, Vercel Cron)
├─ schemas/             care (request · environment) · intake (question · answer contract) · measure · evidence · protocol (activity node) · retrieval · card (Reference card)
├─ intake_cues.py       intake cue table · concept definitions — loader for data/intake_cues.json · goal_definitions.json
├─ instruments.py       measurement instruments — data/instruments loader · presentation per session language · scoring
├─ protocols.py         activity nodes — data/protocols loader · candidate lookup · compact form for the LLM
├─ agent/               LUMI Care Agent (LangGraph)
│  ├─ graph.py          node wiring · routing (v2)
│  ├─ state.py          graph state and node input/output schemas (IntakeResult, CarePlan, Round …)
│  ├─ assembly.py       assembles models · graph from settings (OpenAI → Anthropic fallback)
│  ├─ llm.py            retry (5xx once) · fallback (4xx immediately) rules
│  ├─ context.py        run context — AgentContext (DB pool) · PlannerContext (session values the plan steps read)
│  ├─ checkpoint.py     checkpointers (Postgres · memory) and the types allowed for serialization
│  ├─ prompts.py        intake · Planner prompts and input messages
│  ├─ intake_rules.py   intake rules — question validation · answer merging · environment-question decision · goal · end (tested without an LLM)
│  ├─ intake_texts.py   sentences intake builds in code — fixed questions · options · re-asks · goal reasons (per session language)
│  ├─ nodes/            intake · measure · ① planner · ② executor · ③ evaluator · ④ replanner
│  ├─ validation.py     EvidenceValidationTool core (judge: rules without the DB)
│  ├─ rationale_rules.py character rules for rationales — shared by the plan post-check (one re-ask on a violation) and the rationale evaluation
│  ├─ card_texts.py     sentences the Reference card builds in code — goal name · limit sentences · scale notes (per session language)
│  └─ reference_card.py Reference card — built from validated evidence · protocol · intake (no LLM)
├─ content/             content assets — external providers · reviewed-asset manifests · content Factor catalog (used by the Executor)
├─ static/              reviewed assets — audio (audio) · coloring-template SVGs (coloring), served at /assets
├─ ingestion/           offline evidence building — collect (collect) · Docling section parsing (parse) · source excerpts (extract)
└─ retrieval/           hybrid search (metadata filter → vector · keyword → RRF) · query embedding
data/
├─ protocols/           21 activity node JSON files (filled in by owners A · B · C, review DRAFT)
├─ instruments/         4 measurement instruments (SAM · STAI-6 · BSRI · MFVAS) — en/ holds English wording · status
├─ evidence_overrides.json evidence metadata confirmed by people (overrides the sheet values at load)
├─ evidence_excerpts.json  per-record METHODS · RESULTS excerpts from the papers (used as content at load)
├─ manifest.csv         manifest of 84 papers
├─ intake_cues.json     intake cue table S1·S2·S3 (expression cues · boundaries · summary sources) + state hints
├─ goal_definitions.json intake concept definitions S1·S2·S3 — verbatim sentences from R1 papers (paper_id · page)
├─ eval_scenarios.jsonl 30 evaluation scenarios — goal × user state × phrasing · chips (search-query collection · plan evaluation)
├─ rag_eval_queries.jsonl 220 search queries collected with the production choose prompt (make rag-eval ARGS="collect")
└─ rag_eval.jsonl       evidence retrieval evaluation set, 180 cases — query · filters · gold records (make rag-eval)
scripts/scaffold_protocols.py   workbook → activity node JSON drafts
scripts/seed_evidence.py        evidence-arm sheet → papers · evidence_records load (make seed-evidence)
scripts/plan_trial.py           runs intake → plan scenarios with a real model · collects search queries (make plan-trial)
scripts/rag_eval.py             evidence retrieval evaluation — query collection · case building · Hit@K · Recall@K · MRR · before/after comparison (make rag-eval)
scripts/plan_eval.py            plan evaluation — runs the plan node with a real model per scenario · rule verdicts (make plan-eval)
scripts/rationale_eval.py       rationale evaluation — rules (code) · LLM Judge scoring · run logs · human-label agreement (make rationale-eval)
scripts/rag_compare.py          RAG vs LLM alone vs LLM + web search — citation existence (OpenAlex) · source type · claim-support Judge · comparison table (make rag-compare)
docs/rag-eval/                  evaluation records — dated run logs (script output) and verdict memos (-verdict.md)
scripts/parse_papers.py         paper PDFs → section text cache, Docling · RapidOCR for scans (make parse-papers)
scripts/extract_excerpts.py     per-record METHODS · RESULTS excerpts → data/evidence_excerpts.json (make extract-excerpts)
supabase/migrations/    DB migrations (Supabase CLI file naming)
vercel.json             Vercel deployment settings (region icn1 · cleanup cron)
tests/
├─ agent/               graph flow · Planner wiring · validation rules (scripted models) · validation DB lookups
├─ intake/ measure/ protocols/   per-role tests (TEAM.md)
├─ retrieval/           evidence search (DB tests only with TEST_DATABASE_URL)
└─ schemas/             evidence contract tests (sheet → loaded records only when the workbook is present)
```
