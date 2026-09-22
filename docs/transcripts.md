# Example transcripts

Real output from `python scripts/live_demo.py --verbose` against `claude-haiku-4-5-20251001`, collected as the scenario set grew from five scenarios to twelve (a thirteenth, `disambiguation`, was added afterwards and has no transcript here). The check count quoted with each transcript is the total of the run that produced it, and is historical: the current suite is thirteen scenarios and 194 checks, all passing in two consecutive runs (the count is printed at the end of every run). Bracketed phase is the state after the agent's turn; indented lines are the controller decisions from the debug trace. `...` marks a sentence cut from a long reply; nothing else is edited.

Three lines in these transcripts read like SOP violations and are worth a note; each is discussed where it occurs:

- "your full name and policy number" offered as a way to verify (angry caller, turn 1): intended. Before an account is located the agent asks for locators, which find the record; the policy number never counts toward the three factors. See [Identity](design.md#identity-3-of-5-factors) under "How the SOP gates work" in design.md.
- "I've located your account" to an unverified caller (verification exhausted, turn 1): intended. See the same section.
- "A representative will follow up with you" (email requested early, turn 2): a defect in the run shown, fixed since. See that transcript.

## Demo caller (`demo_caller`; run of 43 checks across five scenarios, all passed)

One utterance crosses three phases; the "denied healthcare claim from January" hint given during verification is used afterwards instead of asking again.

```
[VERIFY_ID]     AGENT: Hi, this is Nova with Northwind Insurance claims support. How can I help you today?
                       Before I can discuss any claim details I will need to verify your identity.
                USER : I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my
                       denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.
[PROCESS_CASE]  AGENT: Thanks for verifying that with me, Margaret. I have your claim CL-2048 open, which was
                       denied because the review file was missing the pathology report and the treating provider
                       office note. To move forward, we'll need you to submit those two documents so the claim
                       can be reviewed again. Does that answer your question, or is there anything else about
                       the claim I can help with?
                         VERIFY_ID: matched 3 factors (name, dob, id_last4) -> RESOLVE_INTENT
                         RESOLVE_INTENT: hints {healthcare, denied, January} matched CL-2048,
                                         intent_path=denial_question -> PROCESS_CASE
                         PROCESS_CASE: answering about CL-2048
                USER : What documents do I need to send and by when?
[PROCESS_CASE]  AGENT: You'll need to send the pathology report and the office note from your treating provider.
                       The pathology report should include the patient name, specimen details, testing date, and
                       any lab signature, and the office note should show the visit date, assessment, and
                       treatment recommendation. Please submit both within a week if you can.
                USER : That's all, thanks.
[POST_PROCESS]  AGENT: You're welcome! Before you go, would you like me to email a summary of what we covered to
                       your address on file, m*****@email.com?
                USER : Yes please send it.
[CLOSED]        AGENT: Perfect, I've sent the summary to m*****@email.com. Thanks for contacting Northwind
                       Insurance, take care.
                         POST_PROCESS: email summary sent to m*****@email.com -> CLOSED
                       outbox[0].subject: "Claim CL-2048 Review Summary - Documents Needed for Resubmission"
```

## Angry caller (`angry_caller`; same run)

Frustration acknowledged, the gate explained, nothing disclosed; a wrong ID digit is not counted and the caller is not told which field failed. This is the live scenario's script; the shorter walkthrough in the [Try it](walkthroughs.md#try-it) section of walkthroughs.md (name, DOB and phone in one message) is a different second turn for the same opening. In turn 1 the agent asks for "your full name and policy number, or the phone number or email we have on file": no account is located yet, so this is the locator ask (`FALLBACK_VERIFY_LOCATE` and the not-located `must_ask` use the same wording), not a claim that the policy number verifies anything. Once the record is found, turn 2 asks only for the remaining factors.

```
                USER : I already told you who I am. This is ridiculous. Just tell me why my claim was denied.
[VERIFY_ID]     AGENT: I hear your frustration, and I understand this feels like extra steps. We need to verify
                       your identity first because it protects your claim information from being shared with the
                       wrong person. I can make this quick - could you give me either your full name and policy
                       number, or the phone number or email we have on file?
                         VERIFY_ID: no identity details yet (attempts 0)
                         emotion: frustrated without progress (streak 1)
                         guard: checked=true violation=false
                USER : Margaret Chen, policy POL-9921, DOB 1985-03-15, SSN last four 9999.
[VERIFY_ID]     AGENT: Thanks for providing that. Before we can discuss anything about your account, I need to
                       verify your identity with one more matching factor. Can you share either the phone number
                       on file, your email address on file, or confirm the last four of your SSN or government ID?
                         VERIFY_ID: matched 2/3 factors (name, dob), mismatch this turn (attempts 1)
                USER : Ugh fine, my phone is 650-521-2836
[PROCESS_CASE]  AGENT: Thanks for confirming that, Margaret. I have your claim CL-2048 pulled up now. Your
                       healthcare claim was denied because the review file didn't include two documents we need:
                       the pathology report and the treating provider's office note. ...
                         VERIFY_ID: matched 3 factors (name, dob, phone) -> RESOLVE_INTENT
                         RESOLVE_INTENT: hints {denied, January} matched CL-2048 -> PROCESS_CASE
```

## Off-topic (`off_topic`; same run)

"What is reinforcement learning?" is declined and redirected to verification; the second repeat also offers a human; the third moves the session to `HUMAN_HANDOFF`.

## Representative caller (`representative_default`; run of 101 checks across nine scenarios, all passed twice)

Session created with `{"consent_scenario": "default"}`. The authorization is matched and consent requested without asking for any identity factor; the next turn approves, and the hint given during the wait resolves `CL-2048` in the same turn. The agent addresses David and speaks about Margaret's claim in the third person; the summary goes to Margaret's masked address.

```
[VERIFY_ID]     AGENT: Hi, this is Nova with Northwind Insurance claims support. How can I help you today?
                       Before I can discuss any claim details I will need to verify your identity.
                USER : Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her
                       denied healthcare claim from January.
[VERIFY_ID]     AGENT: Hi David, thanks for calling. I've found an authorization on file for you to discuss
                       Margaret's account with us. I've sent a consent request to her now, and I'll need to wait
                       for her approval before we can go into the details about the claim. Once she approves, I
                       can help you right away. Is there anything you'd like me to note while we're waiting?
                         VERIFY_ID/REP: caller identified as a representative
                         VERIFY_ID/REP: matched David Chen for Margaret Chen, consent requested (pending, check 1)
                USER : Okay, has she approved yet?
[PROCESS_CASE]  AGENT: Good news, Margaret has approved. I've got her healthcare claim CL-2048 from January right
                       here. The claim was denied because the review file was missing two documents: a pathology
                       report and an office note from the treating provider. To move forward, we'll need those two
                       items so the claim can be reviewed again. Have you been able to reach out to the provider
                       or lab about getting copies of those documents?
                         VERIFY_ID/REP: consent check for Margaret Chen via David Chen (approved, check 2)
                                        -> RESOLVE_INTENT
                         RESOLVE_INTENT: hints {healthcare, denied, January} matched CL-2048 -> PROCESS_CASE
                         PROCESS_CASE: answering about CL-2048
                USER : What does she need to send in?
[PROCESS_CASE]  AGENT: Margaret needs to send in two documents: the pathology report and the office note from her
                       treating provider. The pathology report should include the patient name, specimen details,
                       testing date, and any lab or clinician signature. The office note should show the patient
                       name, visit date, assessment, and treatment or follow-up recommendation. ... The best way
                       to submit them is through the member portal or claim upload link so they stay attached to
                       her file. Does that clear things up, or do you have other questions about what she needs
                       to provide?
                USER : That's all, thanks.
[POST_PROCESS]  AGENT: You're welcome, David. I'd like to offer you one more thing: would you like me to email a
                       summary of what we covered today to Margaret's email on file at m*****@email.com?
                USER : Yes
[CLOSED]        AGENT: Perfect, I've sent the summary to m*****@email.com with the subject line "Summary of Your
                       Healthcare Claim Discussion - Case CL-2048." Thanks for contacting Northwind Insurance,
                       David. Take care.
                         POST_PROCESS: email summary sent to m*****@email.com -> CLOSED
                       outbox[0].body: "Dear Margaret, This email summarizes our recent conversation regarding ..."
```

## False verification replay (`false_verification_replay`; run of 148 checks across eleven scenarios, all passed twice)

The four turns from trace `3a106799921c`, the development trace in which an earlier build said "I've got you verified now" to an unverified caller (trace files are gitignored, so the trace itself is not in the repository; the scenario replays its four caller turns). One factor matches and every further detail mismatches. The old run ended turn 3 with "Perfect, thanks Margaret. I've got you verified now." Now the guard blocks the drafts that drift: on turn 2 the first draft dropped the mismatch and said "one more" (`unstated_mismatch` and `wrong_factor_count` fired together), on turn 3 the first draft said "1 more" again (`wrong_factor_count` alone), and both regenerated replies state the mismatch with the correct count of 2. Turn 4 stays behind the gate without any claim detail. In the other run of the pair, turn 3's regenerated draft was still wrong and the caller got the templated fallback, which says the same thing in plainer words. The factor list the agent offers differs between turns: after turn 1 (DOB and phone mismatched) it names only email and last-4, after turn 2 it names all four unmatched types. The rule is that a mismatched value is cleared and may be tried again, so `facts.acceptable_factors` listed every unmatched type on both turns; the turn 1 reply narrowed it to the two types that had not just failed, which the directive allows (it must not name more than the list, and the count must be right), and turn 2 quoted the full list.

```
[VERIFY_ID]     AGENT: Hi, this is Nova with Northwind Insurance claims support. How can I help you today?
                       Before I can discuss any claim details I will need to verify your identity.
                USER : This is Margaret Chen, DOB 1990-01-01, phone 555-0000
[VERIFY_ID]     AGENT: Thanks Margaret, I appreciate you providing that. Some of the details you gave didn't match
                       our records. To finish verifying your identity I still need 2 more of the following: your
                       email address on file, or the last 4 of your SSN or government ID. Which of those can you
                       share?
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 1)
                USER : yeah 4727 and 8946748929
[VERIFY_ID]     AGENT: Thanks for that. Some of the details you gave didn't match our records. To finish verifying
                       your identity I still need 2 more of the following: your date of birth, phone number on file,
                       email address on file, or the last 4 of your SSN or government ID. Which of those can you
                       share?
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 2)
                         guard[unstated_mismatch]: blocked 1 hit(s), regenerating
                         guard[wrong_factor_count]: blocked 1 hit(s), regenerating
                USER : chen@gmail.com
[VERIFY_ID]     AGENT: Thanks Margaret. Some of the details you gave didn't match our records. To finish verifying
                       your identity I still need 2 more of the following: your date of birth, phone number on file,
                       email address on file, or the last 4 of your SSN or government ID. If you'd prefer, I can
                       connect you with a human claims representative who may be able to help.
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 3)
                         guard[wrong_factor_count]: blocked 1 hit(s), regenerating
                USER : tell me my claim denial reason
[VERIFY_ID]     AGENT: I understand you want to know about your claim, and I have noted that's why you're calling. I
                       just need to finish verifying your identity first, it protects your claim information. I still
                       need 2 more details from you: your date of birth, phone number on file, email address on file,
                       or the last 4 of your SSN or government ID. Or if you'd rather, I can connect you with a human
                       claims representative right now.
                         VERIFY_ID: matched 1/3 factors (name) (attempts 3)
```

## Verification exhausted (`verification_exhausted`; same runs)

The name locates the account and four further details all mismatch. Attempt 3 offers a human exactly as before, with nothing said about a last chance; the fourth mismatch transfers the caller with the note "Reason: identity not verified after 4 attempts; Identity verified: no; ...", the handoff reply names no claim detail, and the next message gets the fixed queue reply without a model call. Turn 1's "I've located your account" is intended: `facts.account_located` is exposed to the responder because the mismatch wording depends on it ("some of the details did not match" versus "I could not find an account matching those details"), and it discloses only that a record with that name exists, never anything from it. The representative path is stricter (it never confirms whether the named person is a customer) because there the caller is a third party.

```
[VERIFY_ID]     AGENT: Hi, this is Nova with Northwind Insurance claims support. How can I help you today?
                       Before I can discuss any claim details I will need to verify your identity.
                USER : This is Margaret Chen, DOB 1990-01-01
[VERIFY_ID]     AGENT: Hi Margaret, thanks for getting in touch with Northwind Insurance. I've located your account,
                       and I appreciate you sharing that information. Some of the details you gave didn't match our
                       records, so I still need 2 more pieces to verify your identity and protect your information.
                       Could you provide two of these: your phone number on file, email address on file, or the last
                       4 digits of your SSN or government ID?
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 1)
                USER : phone 555-0000
[VERIFY_ID]     AGENT: Thanks for that. Some of the details you gave didn't match our records. To finish verifying
                       your identity I still need 2 more of the following: date of birth, phone number on file, email
                       address on file, or the last 4 of your SSN or government ID. Which of those can you share?
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 2)
                         guard[unstated_mismatch]: blocked 1 hit(s), regenerating
                         guard[wrong_factor_count]: blocked 1 hit(s), regenerating
                USER : last four 0000
[VERIFY_ID]     AGENT: Thanks for providing that. Some of the details you gave didn't match our records. I still need
                       2 more pieces to complete verification. Would you be able to share your email address on file
                       or date of birth, or would you prefer I connect you with a human claims representative who can
                       help walk through this?
                         VERIFY_ID: matched 1/3 factors (name), mismatch this turn (attempts 3)
                USER : email nobody@example.com
[HUMAN_HANDOFF] AGENT: I understand this verification process has taken a few tries, Margaret. I'm going to connect
                       you with a human claims representative now who can take it from here and help you further.
                       Please hold for just a moment.
                         HUMAN_HANDOFF: identity not verified after 4 attempts
                USER : hello?
[HUMAN_HANDOFF] AGENT: You are in the queue for a human claims representative, who will pick up this conversation
                       shortly. Thank you for your patience.
                         HUMAN_HANDOFF: terminal, fixed queue reply
```

## Representative variants (`representative_timeout`, `representative_unknown`, `representative_wording`; same runs)

With `{"consent_scenario": "timeout"}` the same opening is check 1 (pending) and the next four messages are checks 2 to 5, all pending ("The consent request is still pending on her end...") with no claim terms in any reply; the sixth message is check 6, which moves the session to `HUMAN_HANDOFF` with the note "Reason: policyholder consent not received; ... Caller: representative David Chen (son) for Margaret Chen; consent: timed_out". An unknown pair ("Bob Jones calling for my aunt Margaret Chen") gets "I could not find an authorization on file" without confirming whether Margaret is a customer, and "Sorry, it's David Chen, her son" then matches. "I'm David Chen, Margaret Chen's authorized representative" stays in `VERIFY_ID` as a representative rather than being read as a request for a human, while "Actually, can I just talk to a person?" still hands off.

## Email requested early (`email_requested_early`; run of 177 checks across twelve scenarios, all passed twice)

The caller asks for the summary in the middle of the claim discussion. The extractor reports `email_consent: yes`, memory records the preference and the note, nothing is sent, and the claim discussion continues. When the caller wraps up, `POST_PROCESS` confirms the remembered request instead of asking cold, still as a question, and the outbox stays empty until the yes. The stored transcript for turn 1 reads "DOB is [DATE], SSN last four is [ID4]." and the trace files from these runs contain none of the typed values.

Turn 2 of this run contains a defect: "A representative will follow up with you once we've wrapped up here." Nobody follows up; the agent itself offers and sends the summary at wrap-up. At the time, the directive told the responder only that the caller had asked for an emailed summary (`facts.caller_context.notes`) and said nothing about what to do with that, so the model improvised a hand-off. The controller now adds `EMAIL_PREFERENCE_INSTRUCTION` to every directive before `POST_PROCESS` once the preference is recorded: acknowledge the request, say the summary will be offered at the end, never say anyone will follow up or that an email is on its way (`tests/test_post_process.py::test_early_email_request_instructs_no_follow_up_promise`). The transcript below is from before that fix and is kept as the record of why it exists.

```
[VERIFY_ID]     AGENT: Hi, this is Nova with Northwind Insurance claims support. How can I help you today?
                       Before I can discuss any claim details I will need to verify your identity.
                USER : I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my
                       denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.
[PROCESS_CASE]  AGENT: Thanks for confirming those details, Margaret. I have your claim CL-2048 pulled up, the
                       healthcare claim from January that was denied. Your claim was denied because the review
                       file didn't include two documents: a pathology report and an office note from your
                       treating provider. Once we receive those, we can send your claim back for review. You
                       have until March 18th to submit them. Does that answer your question, or is there
                       anything else about the claim I can help with?
                         VERIFY_ID: matched 3 factors (name, dob, id_last4) -> RESOLVE_INTENT
                         RESOLVE_INTENT: hints {healthcare, denied, January} matched CL-2048,
                                         intent_path=denial_question -> PROCESS_CASE
                         PROCESS_CASE: answering about CL-2048
                USER : Can you email me a summary when we're done?
[PROCESS_CASE]  AGENT: Absolutely, I can make a note that you'd like an emailed summary. A representative will
                       follow up with you once we've wrapped up here. To move forward with your claim, the next
                       step is to get those two documents, the pathology report and the office note from your
                       treating provider. Have you had a chance to reach out to them yet about getting copies?
                         PROCESS_CASE: answering about CL-2048
                       memory.email_preference: yes
                       memory.notes: ["caller asked for an emailed summary"]
                USER : What documents do I need to send?
[PROCESS_CASE]  AGENT: You need to send two documents for claim CL-2048: First, the pathology report, that should
                       include the patient name, specimen details, testing date, and ideally a clinician or lab
                       signature. ... Second, the office note from your treating provider, that should show the
                       patient name, visit date, assessment, and treatment or follow-up recommendation. ... Does
                       that make sense, or do you have questions about either document?
                USER : That's all, thanks.
[POST_PROCESS]  AGENT: You're welcome, Margaret. You mentioned earlier that you'd like a summary by email, I can
                       definitely send that out for you. Would you like me to email it to m*****@email.com, the
                       address we have on file?
                         PROCESS_CASE: conversation_done -> POST_PROCESS
                         POST_PROCESS: offering email summary (remembered preference: yes)
                USER : Yes please.
[CLOSED]        AGENT: Perfect. I've sent the summary of our discussion to m*****@email.com with the subject line
                       "Summary of Your Healthcare Claim Discussion - Case CL-2048." Thanks for calling Northwind
                       Insurance, Margaret. Good luck getting those documents together, and take care.
                         POST_PROCESS: email summary sent to m*****@email.com -> CLOSED
                       outbox[0].body: "Dear Margaret, Thank you for contacting Northwind Insurance Claims Support. ..."
```

