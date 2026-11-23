// Builds the narrated carousel video (1080x1350, LinkedIn 4:5) and an SRT caption file.
// Each slide holds for its narration plus a little air; slides cross-fade; the R&B bed
// sits under the voice and ducks while it speaks; loudness ends at -14 LUFS.
// Inputs: out/slide-XX.png (render.cjs), build/vo.json (gen_vo.py), build/bgm-seed.wav (gen_bgm.py).
const fs = require("fs");
const path = require("path");
const { execFileSync, spawnSync } = require("child_process");

const here = __dirname;
const build = path.join(here, "build");
const out = path.join(here, "out");
const vo = JSON.parse(fs.readFileSync(path.join(build, "vo.json"), "utf8"));
const XF = 0.5; // cross-fade seconds
const LEAD = 0.6; // pause before the narrator starts on each slide
const TAIL = 0.9; // pause after the narrator finishes

const durations = vo.map((v, i) => LEAD + v.seconds + TAIL + (i === vo.length - 1 ? 2.0 : 0));
const starts = [];
durations.reduce((t, d, i) => {
  starts.push(t);
  return t + d - XF;
}, 0);
const total = starts.at(-1) + durations.at(-1);

const ff = (args) => execFileSync("ffmpeg", ["-v", "error", "-y", ...args], { cwd: build, stdio: "inherit" });

// 1. Video: still slides with cross-fades.
const vIn = vo.flatMap((_, i) => [
  "-loop", "1", "-framerate", "30", "-t", durations[i].toFixed(3),
  "-i", path.join(out, `slide-${String(i + 1).padStart(2, "0")}.png`),
]);
const scale = vo.map((_, i) => `[${i}:v]scale=1080:1350:flags=lanczos,format=yuv420p,setsar=1[s${i}]`);
let prev = "s0";
const xf = [];
for (let i = 1; i < vo.length; i++) {
  const label = i === vo.length - 1 ? "vout" : `x${i}`;
  xf.push(`[${prev}][s${i}]xfade=transition=fade:duration=${XF}:offset=${starts[i].toFixed(3)}[${label}]`);
  prev = label;
}
ff([...vIn, "-filter_complex", [...scale, ...xf].join(";"), "-map", "[vout]", "-r", "30",
  "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "video.mp4"]);

// 2. Narration track.
const aIn = vo.flatMap((v) => ["-i", v.wav]);
const delays = vo.map((v, i) => `[${i}:a]aresample=48000,adelay=${Math.round((starts[i] + LEAD) * 1000)}:all=1[v${i}]`);
ff([...aIn, "-filter_complex",
  `${delays.join(";")};${vo.map((_, i) => `[v${i}]`).join("")}amix=inputs=${vo.length}:normalize=0,apad,atrim=0:${total.toFixed(2)}[vo]`,
  "-map", "[vo]", "vo.wav"]);

// 3. Music bed: five cross-faded copies of the seed, trimmed, faded, ducked under the voice.
const seeds = Array.from({ length: 5 }, () => ["-i", "bgm-seed.wav"]).flat();
ff([...seeds, "-i", "vo.wav", "-filter_complex",
  "[0][1]acrossfade=d=3[a1];[a1][2]acrossfade=d=3[a2];[a2][3]acrossfade=d=3[a3];[a3][4]acrossfade=d=3," +
  "aresample=48000,aformat=channel_layouts=stereo," +
  `atrim=0:${total.toFixed(2)},volume=0.07,afade=t=in:d=1.5,afade=t=out:st=${(total - 3).toFixed(2)}:d=3[bed];` +
  "[5]aformat=channel_layouts=stereo,asplit[vo1][vo2];" +
  "[bed][vo1]sidechaincompress=threshold=0.02:ratio=4:attack=40:release=600[ducked];" +
  "[ducked][vo2]amix=inputs=2:normalize=0[mix]",
  "-map", "[mix]", "-ar", "48000", "premix.wav"]);

// 4. Loudness: two-pass linear loudnorm to -14 LUFS.
const probe = spawnSync("ffmpeg", ["-hide_banner", "-i", "premix.wav", "-af",
  "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], { cwd: build, encoding: "utf8" }).stderr;
const m = JSON.parse(probe.match(/\{[\s\S]*\}/)[0]);
ff(["-i", "premix.wav", "-af",
  `loudnorm=I=-14:TP=-1.5:LRA=11:linear=true:measured_I=${m.input_i}:measured_TP=${m.input_tp}:measured_LRA=${m.input_lra}:measured_thresh=${m.input_thresh}:offset=${m.target_offset}`,
  "-ar", "48000", "mix.wav"]);

// 5. Mux.
const mp4 = path.join(out, "ap-agent-carousel.mp4");
ff(["-i", "video.mp4", "-i", "mix.wav", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
  "-movflags", "+faststart", mp4]);

// 6. Captions: each narration line split into sentences, timed by character share.
const ts = (s) => {
  const ms = Math.round(s * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
};
const cues = [];
vo.forEach((v, i) => {
  const text = v.say.replace(/M C P/g, "MCP");
  const parts = text.match(/[^.!?:]+[.!?:]+/g) || [text];
  const chars = parts.reduce((a, s) => a + s.length, 0);
  let t = starts[i] + LEAD;
  for (const s of parts) {
    const d = (s.length / chars) * v.seconds;
    cues.push([t, t + d, s.trim()]);
    t += d;
  }
});
const srt = cues.map(([a, b, s], k) => `${k + 1}\n${ts(a)} --> ${ts(b)}\n${s}\n`).join("\n");
fs.writeFileSync(path.join(out, "ap-agent-carousel.srt"), srt);
console.log(`wrote ${mp4} (${total.toFixed(1)}s) and ap-agent-carousel.srt (${cues.length} cues)`);
