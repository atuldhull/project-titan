"""Package a cross-encoder reranker as a local int8 ONNX model directory
(Phase 37; §6.1/§6.3 "CPU, ONNX int8").

Downloads the model, exports ONNX if the hub has none, dynamically quantizes
to int8 for the given CPU ISA, and saves a SELF-CONTAINED directory (config +
tokenizer + onnx weights) that ``TITAN_RERANKER_MODEL`` can point at — no hub
access at runtime, and the deploy image can bake the directory in. Requires
the optional ``rerank`` extra (heavy: torch + optimum); run it in a dedicated
venv, never the test venv.

Usage:
    python scripts/fetch_reranker.py --out /models/reranker \\
        [--model cross-encoder/ms-marco-MiniLM-L-6-v2] [--isa avx512_vnni]

Then:
    TITAN_RERANKER_MODEL=/models/reranker
    TITAN_RERANKER_ONNX_FILE=<printed onnx/model_qint8_*.onnx path>
"""

import argparse
import sys
from pathlib import Path

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="HF model id to package")
    parser.add_argument("--out", required=True, help="target model directory")
    parser.add_argument(
        "--isa",
        default="avx512_vnni",
        choices=["arm64", "avx2", "avx512", "avx512_vnni"],
        help="quantization target ISA (avx512_vnni for Zen4/recent Xeon)",
    )
    args = parser.parse_args()

    try:
        from sentence_transformers import CrossEncoder  # noqa: PLC0415 — needs the extra
        from sentence_transformers.backend import (  # noqa: PLC0415
            export_dynamic_quantized_onnx_model,
        )
    except ImportError:
        sys.stderr.write(
            "fetch_reranker needs the `rerank` extra (sentence-transformers[onnx]); "
            "install it in a dedicated venv: uv pip install -e 'apps/api[rerank]'\n"
        )
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"loading {args.model} (backend=onnx; may export if the hub has no ONNX)...")
    model = CrossEncoder(args.model, backend="onnx")
    model.save_pretrained(str(out))
    print(f"quantizing to int8 ({args.isa})...")
    export_dynamic_quantized_onnx_model(model, args.isa, str(out))
    # The artifact name embeds the weights dtype, which differs by preset:
    # optimum's avx2 config quantizes to QUInt8 -> model_quint8_avx2.onnx,
    # while arm64/avx512/avx512_vnni emit model_qint8_<isa>.onnx. Match on
    # THIS run's ISA suffix so a stale artifact from an earlier run with a
    # different ISA can never be selected (deploying VNNI-tuned s8 weights on
    # an AVX2 CPU is the saturation case the avx2 preset exists to avoid).
    quantized = sorted(out.glob(f"onnx/*int8*_{args.isa}.onnx"), key=lambda p: p.stat().st_mtime)
    if not quantized:
        sys.stderr.write(f"quantization produced no onnx/*int8*_{args.isa}.onnx file\n")
        return 1
    rel = quantized[-1].relative_to(out).as_posix()
    print(
        f"packaged {args.model} -> {out}\n"
        f"  TITAN_RERANKER_MODEL={out}\n"
        f"  TITAN_RERANKER_ONNX_FILE={rel}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
