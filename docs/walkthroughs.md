# Running and trying it

Setup options, the four preset walkthroughs with the behaviour to expect, the scripted live check runner, and where the real transcripts are. The [README](../README.md) has the five-minute path through the demo caller; [design.md](design.md) explains the mechanisms these walkthroughs exercise.

## Quick start

You need an API key for an OpenAI-compatible endpoint or for Anthropic. Copy `.env.example` to `.env` and set `LLM_API_KEY`; for an Anthropic key also set `LLM_PROVIDER=anthropic` (the model and base URL then default to Anthropic's).

Option 1, Docker Compose (reads `.env`, publishes port 8000, and mounts `./traces` so trace files land on the host):

```bash
cp .env.example .env        # `copy` on Windows; then edit LLM_API_KEY (and LLM_PROVIDER for Anthropic)
docker compose up --build
```

If port 8000 is already in use, set `APP_PORT` for the host side: `APP_PORT=8010 docker compose up --build` (PowerShell: `$env:APP_PORT=8010; docker compose up --build`); for uvicorn add `--port 8010`.

Option 2, plain Docker (add `-e LLM_PROVIDER=anthropic` for an Anthropic key):

```bash
docker build -t sop-agent .
docker run --rm -p 8000:8000 -e LLM_API_KEY=sk-... sop-agent
```

Option 3, local Python 3.12+ (a virtualenv is recommended; `requirements.txt` includes `pytest`, so this is also all the test suite and the live runner need):

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The first Docker build takes a minute or two, then the log shows `provider=... model=... trace=True` and `Application startup complete`, listening on port 8000. Then open http://localhost:8000: a chat window with the agent's greeting on the left and the SOP State panel on the right, showing phase `VERIFY_ID` and five unchecked identity factors. `GET /healthz` returns `{"status": "ok", "provider": ..., "model": ..., "llm_configured": true|false}`, where `llm_configured` says whether `LLM_API_KEY` was read (not whether the key is valid; that shows on the first message). Without a key the server still starts and serves the UI, but every message gets a fixed "model not configured" reply and the state does not change. With a key the provider rejects (wrong key, wrong provider, retired model), the reply is a fixed notice naming the HTTP status and the variables to check, again with the state unchanged; see [Failure handling](design.md#failure-handling).

## Provider notes

The OpenAI client requests `response_format: json_object` for the extractor call and falls back to prompt-based JSON if the host rejects it with a 400 (common on small self-hosted models). The Anthropic client uses the Messages API with prompt-based JSON. Both use a 30 second timeout with one retry on timeout or 5xx, so a turn that hits every retry (two calls, a guard regeneration and an email draft) can in the worst case take a few minutes before the templated fallback is used; a normal turn with Haiku is a few seconds. The "two consecutive refusing turns" rule under [Identity](design.md#identity-3-of-5-factors) and the 3-of-5 threshold are fixed, not configuration.

## Try it

The UI has eight preset buttons: Demo caller, Representative caller, Angry caller, Partial ID, Off-topic, Wrap up, Email yes, Email no; the first four are the openings of the walkthroughs below. The right-hand SOP State panel shows the current phase, which identity factors matched (checkmarks only, never the values), remembered intent and case hints and notes, the active claim, the off-topic and frustration streaks, the mock email outbox, and the handoff note. `HUMAN_HANDOFF` and `CLOSED` are the end of a session: to try something else, click New session.

Demo caller, one utterance:

> I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

Expect: name, DOB and ID last 4 all match (three checkmarks), so the controller runs `VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE` inside this single turn. The case hints remembered during verification (`healthcare`, `denied`, `January`) match exactly one of Margaret's four claims, `CL-2048` (her only denied one), so the agent does not ask which claim. The reply confirms verification and explains the denial from the record: the review file lacked the pathology report and the treating provider office note. Follow with "What documents do I need to send and by when?" (grounded answer: the two documents and the one-week submission guidance from `required_document_guideline.json`; the appeal deadline on file, 2026-03-18, is in the directive's facts and usually quoted, though the model may answer the "when" with the one-week guidance alone, as in the demo transcript), then "That's all, thanks." (phase `POST_PROCESS`, offer to email a summary to `m*****@email.com`), then "Yes please send it." (phase `CLOSED`, one entry appears in the outbox). The summary has three parts: what was discussed, the claim status or outcome, and the next steps; if the model's draft echoes one of the caller's identity values, a templated version with the same three parts is sent instead.

Angry caller, not verified:

> I already told you who I am. This is ridiculous. Just tell me why my claim was denied.

Expect: phase stays `VERIFY_ID`, zero checkmarks. The reply acknowledges the frustration, explains that verification protects their claim information, asks for the details that locate the account (full name and policy number, or the phone or email on file; no account is located yet, so factors cannot be compared), and says nothing about any claim. The output guard (see [How the SOP gates work](design.md#how-the-sop-gates-work)) would block a claim number, document name, amount or denial reason if the model produced one, and equally a "you're verified now" or "I'm looking at your account". Then "Fine. Margaret Chen, born March 15 1985, phone 650-521-2836." verifies with name, DOB and phone (formats are normalized), and the remembered hint `denied` selects `CL-2048` directly. The live scenario `angry_caller` uses a longer second act (a wrong last-4 first, then the phone); see [transcripts.md](transcripts.md).

Off-topic:

> What is reinforcement learning?

Expect: a one-sentence decline and a return to the current step; `off_topic_streak` goes to 1. Repeating it twice more moves the session to `HUMAN_HANDOFF` with a handoff note.

Representative caller (someone calling for the policyholder):

> Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her denied healthcare claim from January.

Expect: the phase stays `VERIFY_ID` with zero checkmarks, but the caller line in the SOP State panel shows "Caller: representative David Chen (son) - consent: pending" (the relationship shown is whatever the caller stated, or the record's when they did not state one). The controller matched the pair of names against `representatives.json` and sent a (simulated) consent request to Margaret; the reply says so and asks for nothing else, in particular not for Margaret's date of birth or ID. Any next message ("Okay, has she approved yet?") advances the simulated consent: with the default scenario it is approved, the caller line reads "Verified as Margaret Chen via representative David Chen (consent approved)", and the remembered hint resolves `CL-2048` in the same turn, with the reply opening on the approval and addressing David about "her" claim. The "Consent scenario" select next to New session applies to the next session you create: pick `timeout` and the request stays pending for five checks (the opening message is check 1, the next four are checks 2 to 5), then the sixth message hands off to a human with the reason "policyholder consent not received". See [Representative consent flow](design.md#representative-consent-flow).

## Scripted live check

With the server running and a key configured:

```bash
python scripts/live_demo.py                          # all thirteen scenarios against http://localhost:8000
python scripts/live_demo.py --scenario angry_caller  # one scenario
python scripts/live_demo.py --base-url http://localhost:8010 --verbose   # a server started on another port; --verbose prints decisions
```

Scenarios: `demo_caller`, `angry_caller`, `off_topic`, `email_skip`, `partial_id_and_refusal`, `disambiguation`, `representative_default`, `representative_timeout`, `representative_unknown`, `representative_wording`, `false_verification_replay`, `verification_exhausted`, `email_requested_early`. Together they hold 194 checks over 50 caller turns; the total is printed at the end of a run, and the expected result is every check `PASS` and exit code 0 (the transcripts in [transcripts.md](transcripts.md) quote the smaller totals of the older runs that produced them). Measured with Haiku, a run takes about three minutes and just over 100 model calls (two per turn, fewer on handoff-queue turns, plus email drafts and the occasional guard regeneration). A provider outage during a run shows up as `extractor failed: templated fallback, state unchanged` in the decisions and fails that scenario's later state checks; rerun. Each scenario prints the transcript with phases, then PASS/FAIL per check, and the runner exits non-zero on any failure. State checks are strict (phase, verified flags, active claim, outbox length, streak values). Wording checks are tolerant: case-insensitive substring sets, plus a few regexes: the reply must say "some details did not match" on a mismatch turn, must not state a "1 more" count while two factors remain, and on every scenario's final turn must not contain a redaction token (`[EMAIL]`, `[PHONE]`, `[DATE]`, `[ID4]`, which the responder sees in its history and must paraphrase around rather than echo). Representative scenarios create their session with the matching `consent_scenario`. Without a key the agent never leaves `VERIFY_ID`, so most state checks fail; the runner reports each failure, prints a note that the server answered with the not-configured notice, ends with `RESULT: N CHECK(S) FAILED` and exits 1 rather than crashing. A full pass ends with `RESULT: ALL CHECKS PASSED`.

## Example transcripts

Real output from `python scripts/live_demo.py --verbose` against `claude-haiku-4-5-20251001`. The full set (demo, angry, representative, false-verification replay, verification exhausted, representative variants, email requested early), each with the check count of the run that produced it and notes on the three lines that read like SOP violations, is in [transcripts.md](transcripts.md). Its first transcript is the demo caller: one utterance crosses three phases, and the "denied healthcare claim from January" hint given during verification is used afterwards instead of asking again. Bracketed phase is the state after the agent's turn; indented lines are the controller decisions from the debug trace.
