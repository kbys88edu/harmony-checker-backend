from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from music21 import converter, interval
import tempfile
import os


app = FastAPI()

# あとで ateliercompositionson.com だけに制限します
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ateliercompositionson.com",
        "https://www.ateliercompositionson.com",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


VOICE_NAMES = ["Soprano", "Alto", "Tenor", "Bass"]

VOICE_RANGES = {
    "Soprano": {"min": 60, "max": 81, "label": "C4〜A5"},
    "Alto": {"min": 55, "max": 74, "label": "G3〜D5"},
    "Tenor": {"min": 48, "max": 69, "label": "C3〜A4"},
    "Bass": {"min": 40, "max": 64, "label": "E2〜E4"},
}

VOICE_SPACING_LIMITS = {
    ("Soprano", "Alto"): {"max_semitones": 12, "label": "1オクターブ以内"},
    ("Alto", "Tenor"): {"max_semitones": 12, "label": "1オクターブ以内"},
    ("Tenor", "Bass"): {"max_semitones": 24, "label": "2オクターブ以内"},
}

MELODIC_LEAP_LIMITS = {
    "Soprano": {"max_semitones": 12, "label": "1オクターブ以内"},
    "Alto": {"max_semitones": 8, "label": "短6度以内"},
    "Tenor": {"max_semitones": 8, "label": "短6度以内"},
    "Bass": {"max_semitones": 12, "label": "1オクターブ以内"},
}


def get_parts(score):
    parts = score.parts
    if len(parts) < 4:
        raise ValueError("4声のMusicXMLが必要です。Soprano, Alto, Tenor, Bass の4パートを用意してください。")
    return parts[:4]


def get_notes_by_part(parts):
    notes_by_part = []

    for part in parts:
        notes = []
        for n in part.recurse().notes:
            if n.isNote:
                notes.append(n)
            elif n.isChord:
                notes.append(n.notes[-1])
        notes_by_part.append(notes)

    return notes_by_part


def is_perfect_fifth_or_octave(note1, note2):
    iv = interval.Interval(note1, note2)
    simple_name = iv.simpleName

    if simple_name == "P5":
        return "perfect_fifth"
    if simple_name == "P8" or simple_name == "P1":
        return "perfect_octave"
    return None


def direction(note_a, note_b):
    if note_b.pitch.midi > note_a.pitch.midi:
        return "up"
    if note_b.pitch.midi < note_a.pitch.midi:
        return "down"
    return "same"


def melodic_leap_size(note_a, note_b):
    return abs(note_b.pitch.midi - note_a.pitch.midi)


def check_parallel_intervals(notes_by_part):
    results = []

    for i in range(4):
        for j in range(i + 1, 4):
            voice1 = notes_by_part[i]
            voice2 = notes_by_part[j]
            length = min(len(voice1), len(voice2))

            for k in range(length - 1):
                a1, a2 = voice1[k], voice1[k + 1]
                b1, b2 = voice2[k], voice2[k + 1]

                interval_before = is_perfect_fifth_or_octave(a1, b1)
                interval_after = is_perfect_fifth_or_octave(a2, b2)

                if interval_before and interval_after and interval_before == interval_after:
                    dir1 = direction(a1, a2)
                    dir2 = direction(b1, b2)

                    if dir1 == dir2 and dir1 != "same":
                        rule_name = "連続5度" if interval_before == "perfect_fifth" else "連続8度"

                        results.append({
                            "type": "parallel",
                            "rule": rule_name,
                            "voices": [VOICE_NAMES[i], VOICE_NAMES[j]],
                            "measure_before": a1.measureNumber,
                            "measure_after": a2.measureNumber,
                            "message": f"{VOICE_NAMES[i]} と {VOICE_NAMES[j]} に {rule_name} があります。"
                        })

    return results


def check_hidden_intervals(notes_by_part):
    results = []

    for i in range(4):
        for j in range(i + 1, 4):
            upper_voice = notes_by_part[i]
            lower_voice = notes_by_part[j]
            upper_name = VOICE_NAMES[i]
            lower_name = VOICE_NAMES[j]
            length = min(len(upper_voice), len(lower_voice))

            for k in range(length - 1):
                upper_before, upper_after = upper_voice[k], upper_voice[k + 1]
                lower_before, lower_after = lower_voice[k], lower_voice[k + 1]

                before_interval = is_perfect_fifth_or_octave(upper_before, lower_before)
                after_interval = is_perfect_fifth_or_octave(upper_after, lower_after)

                if before_interval and after_interval and before_interval == after_interval:
                    continue

                if after_interval is None:
                    continue

                upper_dir = direction(upper_before, upper_after)
                lower_dir = direction(lower_before, lower_after)

                if upper_dir != lower_dir:
                    continue

                if upper_dir == "same" or lower_dir == "same":
                    continue

                upper_leap = melodic_leap_size(upper_before, upper_after)
                if upper_leap < 3:
                    continue

                rule_name = "隠伏5度" if after_interval == "perfect_fifth" else "隠伏8度"
                severity = "error" if upper_name == "Soprano" and lower_name == "Bass" else "warning"

                results.append({
                    "type": "hidden",
                    "rule": rule_name,
                    "voices": [upper_name, lower_name],
                    "severity": severity,
                    "measure_before": upper_before.measureNumber,
                    "measure_after": upper_after.measureNumber,
                    "message": (
                        f"{upper_name} と {lower_name} に {rule_name} の可能性があります。"
                        f"同方向に進み、到達先が完全5度または完全8度で、上声が跳躍しています。"
                    )
                })

    return results


def check_voice_crossing(notes_by_part):
    results = []
    min_length = min(len(notes) for notes in notes_by_part)

    for k in range(min_length):
        for i in range(3):
            upper_voice = notes_by_part[i][k]
            lower_voice = notes_by_part[i + 1][k]
            upper_name = VOICE_NAMES[i]
            lower_name = VOICE_NAMES[i + 1]

            if upper_voice.pitch.midi < lower_voice.pitch.midi:
                results.append({
                    "type": "voice_crossing",
                    "rule": "声部交差",
                    "voices": [upper_name, lower_name],
                    "measure": upper_voice.measureNumber,
                    "message": f"{upper_name} が {lower_name} より低くなっています。"
                })

    return results


def check_voice_ranges(notes_by_part):
    results = []

    for i, notes in enumerate(notes_by_part):
        voice_name = VOICE_NAMES[i]
        voice_range = VOICE_RANGES[voice_name]

        for n in notes:
            midi = n.pitch.midi
            pitch_name = n.pitch.nameWithOctave

            if midi < voice_range["min"]:
                results.append({
                    "type": "voice_range",
                    "rule": "声域外",
                    "voice": voice_name,
                    "measure": n.measureNumber,
                    "pitch": pitch_name,
                    "message": f"{voice_name} の {pitch_name} は低すぎます。推奨声域は {voice_range['label']} です。"
                })

            elif midi > voice_range["max"]:
                results.append({
                    "type": "voice_range",
                    "rule": "声域外",
                    "voice": voice_name,
                    "measure": n.measureNumber,
                    "pitch": pitch_name,
                    "message": f"{voice_name} の {pitch_name} は高すぎます。推奨声域は {voice_range['label']} です。"
                })

    return results


def check_voice_spacing(notes_by_part):
    results = []
    min_length = min(len(notes) for notes in notes_by_part)

    for k in range(min_length):
        for i in range(3):
            upper_note = notes_by_part[i][k]
            lower_note = notes_by_part[i + 1][k]
            upper_name = VOICE_NAMES[i]
            lower_name = VOICE_NAMES[i + 1]

            spacing_rule = VOICE_SPACING_LIMITS[(upper_name, lower_name)]
            distance = upper_note.pitch.midi - lower_note.pitch.midi

            if distance < 0:
                continue

            if distance > spacing_rule["max_semitones"]:
                results.append({
                    "type": "voice_spacing",
                    "rule": "声部間隔",
                    "voices": [upper_name, lower_name],
                    "measure": upper_note.measureNumber,
                    "distance": distance,
                    "message": (
                        f"{upper_name} と {lower_name} の間隔が広すぎます。"
                        f"現在 {distance} 半音離れています。目安は {spacing_rule['label']} です。"
                    )
                })

    return results


def check_melodic_leaps(notes_by_part):
    results = []

    for i, notes in enumerate(notes_by_part):
        voice_name = VOICE_NAMES[i]
        limit = MELODIC_LEAP_LIMITS[voice_name]

        for k in range(len(notes) - 1):
            note_before = notes[k]
            note_after = notes[k + 1]
            leap = melodic_leap_size(note_before, note_after)

            if leap > limit["max_semitones"]:
                direction_label = "上行" if note_after.pitch.midi > note_before.pitch.midi else "下行"

                results.append({
                    "type": "melodic_leap",
                    "rule": "大きすぎる跳躍",
                    "voice": voice_name,
                    "measure_before": note_before.measureNumber,
                    "measure_after": note_after.measureNumber,
                    "pitch_before": note_before.pitch.nameWithOctave,
                    "pitch_after": note_after.pitch.nameWithOctave,
                    "leap": leap,
                    "message": (
                        f"{voice_name} に大きすぎる跳躍があります。"
                        f"{note_before.pitch.nameWithOctave} から {note_after.pitch.nameWithOctave} へ"
                        f"{direction_label}し、{leap} 半音動いています。目安は {limit['label']} です。"
                    )
                })

    return results


def analyze_musicxml(path):
    score = converter.parse(path)
    parts = get_parts(score)
    notes_by_part = get_notes_by_part(parts)

    results = []
    results.extend(check_parallel_intervals(notes_by_part))
    results.extend(check_hidden_intervals(notes_by_part))
    results.extend(check_voice_crossing(notes_by_part))
    results.extend(check_voice_ranges(notes_by_part))
    results.extend(check_voice_spacing(notes_by_part))
    results.extend(check_melodic_leaps(notes_by_part))

    return results


@app.get("/")
def root():
    return {"status": "Harmony Checker API is running"}


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename)[1] or ".musicxml"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        results = analyze_musicxml(tmp_path)
        return {
            "ok": True,
            "filename": file.filename,
            "count": len(results),
            "results": results
        }
    except Exception as e:
        return {
            "ok": False,
            "filename": file.filename,
            "error": str(e),
            "results": []
        }
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)