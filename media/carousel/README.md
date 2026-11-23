# LinkedIn carousel and narrated video

Ten slides (1080x1350) about the AP agent, as a PDF for a LinkedIn document post and as a
2-minute narrated MP4 with captions. Every number on the slides comes from `EVALS.md`, the
live sandbox run log, or real CLI output captured from the demo.

Outputs land in `out/` (not committed): `ap-agent-carousel.pdf`, `ap-agent-carousel.mp4`,
`ap-agent-carousel.srt` and one PNG per slide.

## Rebuild

1. Capture real output and screenshots (needs the review page running on port 8811 against
   the carousel state; see the commands at the top of each script):
   `build/cli.txt`, then `node capture_review.cjs <proposal-id> build`
2. Slides and PDF: `node render.cjs`
3. Narration, free and local: `C:\Python314\python.exe gen_vo.py`
   (Kokoro TTS, voice af_heart, Apache-2.0 model)
4. Music bed, free and local: the `gen_bgm.py` recipe from the Process X-Ray demo
   (MusicGen small, instrumental R&B), saved as `build/bgm-seed.wav`
5. Video and captions: `node assemble.cjs`

Set `PLAYWRIGHT_PATH` to a Playwright install for steps 1 and 2.
