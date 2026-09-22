# Media

- `demo.png`: policyholder "Demo caller" flow. Identity verified on one turn (name, DOB, ID last 4 checked), phase `PROCESS_CASE`, agent explains the denial of claim CL-2048 and answers "What documents do I need to send and by when?".
- `representative.png`: "Representative caller" flow. David Chen calls for his mother; the controller finds the authorization, requests consent, and only discusses the claim after consent is approved (state panel shows the representative line and consent status).
- To record `demo.gif` (~25 s): start the app, open http://localhost:8000, press Win+Shift+R (Snipping Tool screen recording) or use ScreenToGif, then: click "Demo caller" and send; send "What documents do I need to send and by when?"; send "That's all, thanks."; send "Yes please send it." (the Email outbox card fills in). Export as `demo.gif` into this folder.
- Once `demo.gif` exists, switch the image reference in the top-level README from `docs/media/demo.png` to `docs/media/demo.gif`.
