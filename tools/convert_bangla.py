"""Build the Bengali speech model: mozilla-ai/whisper-large-v3-bn (a Bengali fine-tune of Whisper large-v3,
Apache-2.0) converted to CTranslate2 float16 for faster-whisper. setup.ps1 -Bangla runs this in a throw-away
environment (torch on the CPU, transformers, ctranslate2, msvc-runtime) and deletes that environment afterwards.

    python convert_bangla.py <output dir> <large-v3 tokenizer.json>

Lessons from the first build (Sep 15 2026): msvc-runtime's DLLs must be on the DLL path before ctranslate2 loads;
the repo has no tokenizer.json (same multilingual vocabulary as large-v3 - copied from it, else faster-whisper
tries to download one at load); --low_cpu_mem_usage keeps the 6 GB of float32 weights from needing 12 GB of RAM."""
import os
import shutil
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    out, tokenizer = sys.argv[1], sys.argv[2]
    env = sys.prefix
    for d in (env, os.path.join(env, "Scripts")):
        if os.path.isdir(d):
            os.add_dll_directory(d)
    os.environ["PATH"] = os.pathsep.join([env, os.path.join(env, "Scripts"), os.environ.get("PATH", "")])

    from ctranslate2.converters.transformers import main as convert
    sys.argv = ["ct2-transformers-converter", "--model", "mozilla-ai/whisper-large-v3-bn", "--output_dir", out,
                "--quantization", "float16", "--copy_files", "preprocessor_config.json", "--low_cpu_mem_usage",
                "--force"]
    convert()
    shutil.copy(tokenizer, os.path.join(out, "tokenizer.json"))
    print("Bengali model written to", out, sorted(os.listdir(out)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
