"""Who is talking: speaker turns for `sequenceguider notes --speakers`.

A workshop recording often has more than one voice: two teachers (a man and a
woman) speaking English and an interpreter repeating it in Korean. You say who
is there; this finds where each one talks:

1. speech     voice activity (Silero VAD, shipped with faster-whisper) cut into
              pieces of a few seconds
2. voices     a voice fingerprint per piece (WeSpeaker ResNet34, ONNX, ~25MB
              downloaded once) clustered into a few groups of similar voices
3. who        each group is matched to a speaker you named by its language
              (Whisper's language detection on a sample of the group) and how
              high it sounds (median voice pitch)

The turns then tell the transcription which language each stretch is in (so the
interpreter's Korean is written in Korean, not "translated" into English) and
whose line each subtitle is.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SR = 16000
EMBED_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
EMBED_FILE = "voxceleb_resnet34_LM.onnx"
PIECE_S = 6.0      # longest stretch given a single speaker
MIN_EMBED_S = 1.0  # shorter pieces take the speaker of their neighbours

GENDERS = {"male": "male", "m": "male", "남": "male", "남자": "male", "남성": "male",
           "female": "female", "f": "female", "여": "female", "여자": "female", "여성": "female"}
LANGUAGES = {"ko": "ko", "en": "en", "es": "es", "ja": "ja", "fr": "fr", "it": "it", "de": "de", "pt": "pt", "zh": "zh",
             "ru": "ru", "한국어": "ko", "한글": "ko", "한": "ko", "korean": "ko", "영어": "en", "영": "en", "english": "en",
             "스페인어": "es", "spanish": "es", "일본어": "ja", "프랑스어": "fr", "이탈리아어": "it", "독일어": "de",
             "포르투갈어": "pt", "중국어": "zh", "러시아어": "ru"}
_HANGUL = re.compile(r"[가-힣]")
_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class Speaker:
    name: str
    gender: str = ""    # male | female | "" (unknown)
    language: str = ""  # ko, en, es ... | "" (unknown)


@dataclass
class Turn:
    start: float
    end: float
    speaker: str
    language: str


def parse_speakers(spec: str) -> list[Speaker]:
    """'남자 강사=남/en, 여자 강사=여/en, 통역=남/ko' → speakers.

    After '=' come a gender (남/여/male/female) and/or a language (en, ko, 영어, 한국어),
    in any order, separated by '/' or spaces.
    """
    speakers = []
    for part in re.split(r"[,\n]", spec):
        if not part.strip():
            continue
        name, _, attrs = part.partition("=")
        sp = Speaker(name.strip())
        if not sp.name:
            raise ValueError('화자 이름이 비어 있습니다. 예: --speakers "남자 강사=남/en, 여자 강사=여/en, 통역=남/ko"')
        for word in re.split(r"[/\s]+", attrs.strip().lower()):
            if word in GENDERS:
                sp.gender = GENDERS[word]
            elif word in LANGUAGES:
                sp.language = LANGUAGES[word]
            elif word:
                raise ValueError(f"화자 '{sp.name}'의 '{word}'를 알 수 없습니다. 성별(남/여)과 언어(en, ko ...)만 적어 주세요.")
        speakers.append(sp)
    if len({s.name for s in speakers}) != len(speakers):
        raise ValueError("화자 이름이 겹칩니다.")
    return speakers


def is_korean(text: str) -> bool:
    letters = _LETTER.findall(text)
    return bool(letters) and len(_HANGUL.findall(text)) >= 0.3 * len(letters)


# ---------------------------------------------------------------- pitch

def voice_pitch(samples: np.ndarray, sr: int = SR) -> float:
    """Median fundamental frequency (Hz) of the voiced frames; 0 if there are too few.

    YIN (cumulative-mean-normalised difference, from the autocorrelation) on 40 ms
    frames, 60–400 Hz. Rough on a noisy dance floor, but the median over a group of
    pieces tells a low voice from a high one.
    """
    x = np.asarray(samples, dtype=np.float32)
    win, hop = int(0.04 * sr), int(0.01 * sr)
    if len(x) < win:
        return 0.0
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    frames = frames - frames.mean(axis=1, keepdims=True)
    energy = (frames ** 2).mean(axis=1)
    frames = frames[energy > max(1e-7, 0.5 * np.median(energy))]  # the louder half: skip pauses and breaths
    if not len(frames):
        return 0.0
    n = 1 << (2 * win - 1).bit_length()
    spec = np.fft.rfft(frames, n)
    acf = np.fft.irfft(spec * np.conj(spec), n)[:, :win]
    # difference function d(tau) = sum (x[j] - x[j+tau])^2 over the overlap, from the autocorrelation
    sq = np.concatenate([np.zeros((len(frames), 1)), np.cumsum(frames.astype(np.float64) ** 2, axis=1)], axis=1)
    tau = np.arange(win)
    d = sq[:, win - tau] + (sq[:, win:win + 1] - sq[:, tau]) - 2 * acf
    d[:, 0] = 0
    cmnd = d * np.arange(win) / np.maximum(np.cumsum(d, axis=1), 1e-9)
    lo, hi = int(sr / 400), int(sr / 60)
    band = cmnd[:, lo:hi]
    out = []
    for row in band:
        below = np.flatnonzero(row < 0.2)
        if not len(below):
            continue  # unvoiced, or music louder than the voice
        i = below[0]
        while i + 1 < len(row) and row[i + 1] < row[i]:  # down to the dip's minimum
            i += 1
        out.append(sr / (lo + i))
    return float(np.median(out)) if len(out) >= 5 else 0.0


# ---------------------------------------------------------------- voice fingerprints

def _mel_banks(n_mels: int = 80, n_fft: int = 512, sr: int = SR, low: float = 20.0) -> np.ndarray:
    """Kaldi's triangular mel filters (what WeSpeaker was trained on), shape (n_mels, n_fft/2)."""
    mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)  # noqa: E731
    lo, hi = mel(low), mel(sr / 2)
    delta = (hi - lo) / (n_mels + 1)
    m = mel(np.arange(n_fft // 2) * sr / n_fft)
    left = lo + delta * np.arange(n_mels)[:, None]
    center, right = left + delta, left + 2 * delta
    up, down = (m - left) / (center - left), (right - m) / (right - center)
    return np.where((m > left) & (m < right), np.where(m <= center, up, down), 0.0)


_MELS = None


def fbank(samples: np.ndarray) -> np.ndarray:
    """80-dim log mel filterbank, Kaldi style (25 ms / 10 ms, povey window), mean-normalised."""
    global _MELS
    if _MELS is None:
        _MELS = _mel_banks()
    x = np.asarray(samples, dtype=np.float64) * 32768
    if len(x) < 400:
        return np.zeros((0, 80), dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, 400)[::160]
    frames = frames - frames.mean(axis=1, keepdims=True)
    pre = frames.copy()
    pre[:, 1:] -= 0.97 * frames[:, :-1]
    pre[:, 0] -= 0.97 * frames[:, 0]
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85
    power = np.abs(np.fft.rfft(pre * window, 512)) ** 2
    feats = np.log(np.maximum(power[:, :256] @ _MELS.T, np.finfo(np.float32).eps))
    return (feats - feats.mean(axis=0)).astype(np.float32)


class Embedder:
    """WeSpeaker voice fingerprints through onnxruntime (installed with faster-whisper)."""

    def __init__(self):
        import onnxruntime
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(EMBED_REPO, EMBED_FILE)
        opts = onnxruntime.SessionOptions()
        opts.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(path, opts, providers=["CPUExecutionProvider"])

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        feats = fbank(samples)
        emb = self.session.run(None, {"feats": feats[None]})[0][0]
        return emb / (np.linalg.norm(emb) + 1e-9)


# ---------------------------------------------------------------- clustering

def _kmeans(x: np.ndarray, k: int, restarts: int = 8, iters: int = 50, seed: int = 0) -> np.ndarray:
    """Spherical k-means (cosine) with k-means++ starts; returns a label per row."""
    rng = np.random.default_rng(seed)
    best, best_score = np.zeros(len(x), dtype=int), -np.inf
    k = min(k, len(x))
    for _ in range(restarts):
        centers = [x[rng.integers(len(x))]]
        for _ in range(1, k):
            dist = 1 - np.max(x @ np.array(centers).T, axis=1)
            p = np.maximum(dist, 0) ** 2
            centers.append(x[rng.choice(len(x), p=p / p.sum())] if p.sum() > 0 else x[rng.integers(len(x))])
        c = np.array(centers)
        for _ in range(iters):
            labels = np.argmax(x @ c.T, axis=1)
            new = np.array([x[labels == j].sum(axis=0) if (labels == j).any() else c[j] for j in range(k)])
            new /= np.linalg.norm(new, axis=1, keepdims=True) + 1e-9
            if np.allclose(new, c):
                break
            c = new
        score = float(np.sum(np.max(x @ c.T, axis=1)))
        if score > best_score:
            best, best_score = labels, score
    return best


# ---------------------------------------------------------------- diarize

def speech_pieces(audio: np.ndarray) -> list[tuple[float, float]]:
    """Where someone talks, in pieces of at most PIECE_S seconds."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    pieces = []
    for r in get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=100)):
        a, b = r["start"] / SR, r["end"] / SR
        n = max(1, int(np.ceil((b - a) / PIECE_S)))
        step = (b - a) / n
        pieces += [(a + i * step, a + (i + 1) * step) for i in range(n)]
    return pieces


def diarize(audio: np.ndarray, speakers: list[Speaker], detect_language: Callable[[np.ndarray], dict[str, float]],
            *, embedder: Embedder | None = None, extra_groups: int = 2,
            progress: Callable[[float, float], None] | None = None,
            log: Callable[[str], None] | None = None) -> list[Turn]:
    """Speaker turns over the whole recording.

    1. language   every piece, when the speakers don't all speak the same one:
                  `detect_language(samples)` → {language: probability} (Whisper's)
    2. voices     fingerprints of the pieces long enough to have one, clustered into a
                  few more groups than there are speakers (students, music and the
                  room's echo make groups of their own)
    3. who        a piece goes to the speaker of its language; between speakers of the
                  same language (the two teachers) by whether its group is the high or
                  the low voice. Pieces whose language is unclear take their group's.
    """
    pieces = speech_pieces(audio)
    if not pieces:
        return []
    embedder = embedder or Embedder()
    clip = lambda a, b: audio[int(a * SR): int(b * SR)]  # noqa: E731
    dur = np.array([b - a for a, b in pieces])
    long = [i for i in range(len(pieces)) if dur[i] >= MIN_EMBED_S]
    wanted = sorted({p.language for p in speakers if p.language})
    multi = len(wanted) > 1
    steps, done = len(long) + (len(pieces) if multi else 0), 0

    lang = [wanted[0] if wanted else ""] * len(pieces)
    sure = np.zeros(len(pieces), dtype=bool)
    if multi:
        for i, (a, b) in enumerate(pieces):
            probs = detect_language(clip(a, b))
            lang[i] = max(wanted, key=lambda w: probs.get(w, 0.0))
            sure[i] = probs.get(lang[i], 0.0) >= 0.5
            done += 1
            if progress:
                progress(done, steps)
    emb, f0 = [], np.zeros(len(pieces))
    for i in long:
        emb.append(embedder(clip(*pieces[i])))
        f0[i] = voice_pitch(clip(*pieces[i]))
        done += 1
        if progress:
            progress(done, steps)

    # every piece's voice group (a short one: its nearest long neighbour's)
    group = np.zeros(len(pieces), dtype=int)
    if long:
        labels = _kmeans(np.array(emb), len(speakers) + extra_groups)
        group[long] = labels
        starts = np.array([pieces[i][0] for i in long])
        for i in range(len(pieces)):
            if dur[i] < MIN_EMBED_S:
                group[i] = labels[int(np.argmin(np.abs(starts - pieces[i][0])))]
    groups = sorted(set(group.tolist()))
    info = {}  # group → (language by the most confident talking time, median pitch, minutes)
    for g in groups:
        members = np.flatnonzero(group == g)
        votes = {w: dur[[i for i in members if sure[i] and lang[i] == w]].sum() for w in wanted}
        g_lang = max(votes, key=votes.get) if votes and max(votes.values()) > 0 else (wanted[0] if wanted else "")
        pitches = f0[members][f0[members] > 0]
        info[g] = (g_lang, float(np.median(pitches)) if len(pitches) else 0.0, dur[members].sum() / 60)

    def gender(g: int) -> str:
        """High or low voice, relative to the other sizeable groups of the same language."""
        g_lang, pitch, _ = info[g]
        same = [p for lg, p, mins in info.values() if lg == g_lang and p > 0 and mins >= 0.5]
        if not pitch:
            return ""
        if len(same) < 2 or max(same) / min(same) < 1.2:  # no clear split: absolute pitch
            return "female" if pitch > 165 else "male"
        return "female" if pitch > np.sqrt(min(same) * max(same)) else "male"

    def who(i: int) -> Speaker:
        piece_lang = lang[i] if sure[i] or not multi else info[group[i]][0]
        cands = [p for p in speakers if not p.language or p.language == piece_lang] or speakers
        if len(cands) > 1:
            gd = gender(group[i])
            cands = [p for p in cands if p.gender == gd] or cands
        return cands[0]

    owners = [who(i) for i in range(len(pieces))]
    if log:
        for g in groups:
            g_lang, pitch, mins = info[g]
            names = [owners[i].name for i in np.flatnonzero(group == g)]
            top = max(set(names), key=names.count)
            log(f"목소리 묶음 {g + 1}: {mins:.1f}분, {g_lang or '?'}, {pitch:.0f}Hz → 주로 {top}")

    turns: list[Turn] = []
    for (a, b), sp, i in zip(pieces, owners, range(len(pieces))):
        piece_lang = sp.language or lang[i]
        if turns and turns[-1].speaker == sp.name and a - turns[-1].end < 1.0:
            turns[-1].end = round(b, 2)
        else:
            turns.append(Turn(round(a, 2), round(b, 2), sp.name, piece_lang))
    return turns


def language_runs(turns: list[Turn], max_gap: float = 2.0) -> list[Turn]:
    """Consecutive turns in the same language joined (the speaker field is the first one's)."""
    runs: list[Turn] = []
    for t in turns:
        if runs and runs[-1].language == t.language and t.start - runs[-1].end <= max_gap:
            runs[-1].end = t.end
        else:
            runs.append(Turn(t.start, t.end, t.speaker, t.language))
    return runs


def speaker_at(turns: list[Turn], start: float, end: float) -> str:
    """The speaker who talks most between start and end (the nearest turn if none overlaps)."""
    if not turns:
        return ""
    best, best_overlap = None, 0.0
    for t in turns:
        overlap = min(end, t.end) - max(start, t.start)
        if overlap > best_overlap:
            best, best_overlap = t, overlap
    if best is None:
        mid = (start + end) / 2
        best = min(turns, key=lambda t: min(abs(t.start - mid), abs(t.end - mid)))
    return best.speaker
