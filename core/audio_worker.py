"""Run native speech libraries outside the desktop process. The parent owns transcript saving."""
import json
import os
import sys

import audio_engine as engine


def main():
    # Conda NumPy/MKL can load Intel OpenMP alongside CTranslate2's LLVM
    # OpenMP on Windows. MKL's sequential backend avoids that fatal conflict.
    if os.name == "nt":
        os.environ["MKL_THREADING_LAYER"] = "SEQUENTIAL"
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def send(kind, *values):
        output.write(json.dumps([kind, *values]) + "\n")
        output.flush()

    try:
        request = json.load(sys.stdin)
        if request["stage"] == "Transcription":
            result = engine.transcribe_file(
                request["path"], request["model"], request["language"],
                on_progress=lambda *v: send("progress", *v), words=request["words"],
                on_segment=lambda seg: send("segment", seg), on_status=lambda msg: send("status", msg),
                conservative=request["conservative"], hint=request.get("hint"))
            del result["audio"]
        else:
            result = engine.diarize(engine._decode_with_av(request["path"]), request["num_speakers"],
                                    request["token"], on_step=lambda *v: send("step", *v))
        send("result", result)
    except Exception as exc:
        send("error", f"{type(exc).__name__}: {exc}")
    finally:
        output.close()


if __name__ == "__main__":
    main()
