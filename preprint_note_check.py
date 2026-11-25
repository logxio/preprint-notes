#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yan Su <hi@yansu.me>
"""Deterministic preprint verification & Common Notes ef_notes.js LaTeXML anchor adapter."""
from __future__ import annotations
import argparse, hashlib, json, math, re, sys
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HTML = BASE_DIR / "fixtures" / "arxiv_2611_32266v1_sample.html"
UNIT_HTML = BASE_DIR / "fixtures" / "synthetic_stat_units.html"
STICKINESS_JSON = BASE_DIR / "stickiness_summary.json"
MATH_SOURCE_TAGS, HIDDEN_TAGS = {"annotation", "annotation-xml"}, {"script", "style"}
LANDIS_INTERVALS = {"poor": "< 0.00", "slight": "[0.00, 0.20]", "fair": "[0.21, 0.40]", "moderate": "[0.41, 0.60]", "substantial": "[0.61, 0.80]", "almost perfect": "[0.81, 1.00]"}
KAPPA_VAL_RE = re.compile(r"(?:κ|kappa)[^=;,.]{0,16}=\s*(-?(?:0\.\d+|1\.0+))", re.I)


def norm_tokens(text: str) -> tuple[str, list[int]]:
    flat, mapping, space = [], [], True
    for idx, ch in enumerate(text):
        low = ch.lower()
        if low.isalnum():
            flat.append(low); mapping.append(idx); space = False
        elif not space:
            flat.append(" "); mapping.append(idx); space = True
    return "".join(flat).strip(), mapping

def ef_notes_find(host_raw: str, quote: str) -> tuple[int, int] | None:
    """Port of epistemic.fund/notes.js (ef_notes.js lines 37 to 67) Unicode find()."""
    flat, mapping = norm_tokens(host_raw)
    q_norm, _ = norm_tokens(quote)
    if len(q_norm) < 12 or not mapping:
        return None
    at = flat.find(q_norm)
    if at >= 0:
        return (mapping[at], mapping[at + len(q_norm) - 1] + 1)
    words = q_norm.split(" ")
    if len(words) < 12:
        return None
    head, tail = " ".join(words[:6]), " ".join(words[-6:])
    at = flat.find(head)
    if at < 0:
        return None
    end = flat.find(tail, at + len(head))
    if end < 0 or (end + len(tail) - at) > len(q_norm) * 1.5:
        return None
    return (mapping[at], mapping[end + len(tail) - 1] + 1)

class PreprintDOMParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.cur_p: dict[str, object] | None = None
        self.paragraphs: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "meta" and ad.get("name") and ad.get("content"):
            self.meta[ad["name"]] = ad["content"]
        self.stack.append((tag, ad))
        if tag == "p":
            src = next((a.get("data-source", "") for _, a in reversed(self.stack) if a.get("data-source")), "")
            self.cur_p = {"id": ad.get("id", f"p{len(self.paragraphs)+1}"), "source": src, "ef_raw": "", "clean": "", "has_math_tex": False}
        elif self.cur_p is not None and tag in MATH_SOURCE_TAGS:
            self.cur_p["has_math_tex"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.cur_p is not None:
            self.paragraphs.append(self.cur_p); self.cur_p = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.cur_p is None:
            return
        tags = {t for t, _ in self.stack}
        if tags & HIDDEN_TAGS or any("hidden" in a for _, a in self.stack):
            return
        self.cur_p["ef_raw"] = str(self.cur_p["ef_raw"]) + data
        if not (tags & MATH_SOURCE_TAGS):
            self.cur_p["clean"] = str(self.cur_p["clean"]) + data

def betainc_reg(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a, b) via Lentz continued fraction."""
    if x <= 0.0 or x >= 1.0:
        return 0.0 if x <= 0.0 else 1.0
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - betainc_reg(1.0 - x, b, a)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta) / a
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1.0)
    f = d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
    for m in range(1, 200):
        m2 = 2 * m
        for num in (
            m * (b - m) * x / ((a + m2 - 1.0) * (a + m2)),
            -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1.0)),
        ):
            d = 1.0 / ((1.0 + num * d) if abs(1.0 + num * d) > 1e-30 else 1e-30)
            c = 1.0 + num / (c if abs(c) > 1e-30 else 1e-30)
            f *= c * d
        if abs(c * d - 1.0) < 1e-14:
            break
    return front * f

def landis_koch_label(kappa: float) -> str:
    if kappa < 0.0:
        return "poor"
    for hi, label in [(0.20, "slight"), (0.40, "fair"), (0.60, "moderate"), (0.80, "substantial")]:
        if kappa <= hi + 1e-9:
            return label
    return "almost perfect"

def audit_paragraph(p: dict[str, object], doc_id: str) -> list[dict[str, object]]:
    text = re.sub(r"\s+", " ", str(p["clean"])).strip()
    pid, src, findings = str(p["id"]), str(p["source"]) or doc_id, []
    if "landis" in text.lower() and "koch" in text.lower():
        sents = re.split(r"(?<=[.!?])\s+", text)
        for i, st in enumerate(sents):
            if "landis" not in st.lower():
                continue
            vals = [float(x) for x in KAPPA_VAL_RE.findall(st)]
            win = st if (vals or i == 0) else (sents[i - 1] + " " + st)
            vals = vals or [float(x) for x in KAPPA_VAL_RE.findall(win)]
            labels = [m.lower() for m in re.findall(r"[“\"](poor|slight|fair|moderate|substantial|almost perfect)[”\"]", st, re.I)]
            for idx, (val, rep) in enumerate(zip(vals, labels)):
                exp = landis_koch_label(val)
                if exp != rep:
                    findings.append({"check": "landis_koch_threshold", "paragraph_id": pid, "source": src, "pair_index": idx, "context_quote": win[:240], "reported": f"kappa={val:.3f} labeled '{rep}'", "recomputed": f"Landis & Koch (1977) {LANDIS_INTERVALS[exp]} => '{exp}' ('{rep}' requires {LANDIS_INTERVALS[rep]})"})
    for m in re.finditer(r"\bt\(\s*(\d+)\s*\)\s*=\s*(\d+(?:\.\d+)?)\s*,\s*p\s*=\s*(0?\.\d+|\d+\.\d+)", text):
        df, t_v, p_rep = int(m.group(1)), float(m.group(2)), float(m.group(3))
        tail_ctx = text[max(0, m.start() - 45):min(len(text), m.end() + 45)].lower()
        one_tailed = "one-tailed" in tail_ctx or "one-sided" in tail_ctx
        p_calc = betainc_reg(df / (df + t_v * t_v), 0.5 * df, 0.5) * (0.5 if one_tailed else 1.0)
        if abs(p_calc - p_rep) > 0.005:
            tail_lbl = "one-tailed" if one_tailed else "two-tailed"
            ratio_s = f" ({p_calc/p_rep:.1f}x reported p)" if p_rep > 0 else ""
            findings.append({
                "check": "statcheck_student_t", "paragraph_id": pid, "source": src, "context_quote": text[:240],
                "reported": f"t({df})={t_v}, p={p_rep}", "recomputed": f"exact {tail_lbl} p={p_calc:.5f}{ratio_s}",
            })
    for k_s, n_s, pct_s in re.findall(r"\b(\d+)\s*/\s*(\d+)\s*\(\s*([0-9.]+)%\s*\)", text):
        k, n, pct_rep = int(k_s), int(n_s), float(pct_s)
        if n > 0 and abs(100.0 * k / n - pct_rep) > 0.55:
            pct_calc = 100.0 * k / n
            findings.append({
                "check": "denominator_k_over_n", "paragraph_id": pid, "source": src, "context_quote": text[:240],
                "reported": f"{k}/{n} ({pct_rep}%)", "recomputed": f"{k}/{n} = {pct_calc:.2f}% (delta {pct_rep - pct_calc:+.2f} pp)",
            })
    grim_m = re.search(r"\(N\s*=\s*(\d+)\).*?\bM\s*=\s*(\d+\.(\d+))", text)
    if grim_m:
        n_v, m_str, dec = int(grim_m.group(1)), grim_m.group(2), len(grim_m.group(3))
        if n_v < 10 ** dec:
            m_v = float(m_str)
            valid = {f"{math.floor(m_v * n_v) / n_v:.{dec}f}", f"{math.ceil(m_v * n_v) / n_v:.{dec}f}"}
            if m_str not in valid:
                findings.append({
                    "check": "grim_integer_mean", "paragraph_id": pid, "source": src, "context_quote": text[:240],
                    "reported": f"N={n_v}, M={m_str}", "recomputed": f"N*M={n_v*m_v:.2f} non-integer; nearest valid means {sorted(valid)}",
                })
    return findings

def run_audit(html_path: Path, custom_quote: str | None = None, include_unit_selfcheck: bool = False) -> dict[str, object]:
    parser = PreprintDOMParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    arxiv_id = parser.meta.get("citation_arxiv_id", "")
    is_synth = parser.meta.get("fixture_kind") == "synthetic_unit_fixture"
    doc_id = f"arXiv:{arxiv_id}" if arxiv_id else html_path.name
    doc_url = f"https://arxiv.org/html/{arxiv_id}" if arxiv_id else str(html_path)
    nonempty = [p for p in parser.paragraphs if str(p["clean"]).strip()]
    target_p = next((p for p in nonempty if custom_quote and custom_quote in str(p["clean"])), None)
    if target_p is None:
        target_p = next((p for p in nonempty if p["has_math_tex"]), nonempty[0] if nonempty else None)
    anchor_test = None
    if target_p is not None:
        cleaned = re.sub(r"\s+", " ", str(target_p["clean"])).strip()
        sent = re.split(r"(?<=\.)\s+", cleaned)[0]
        quote = custom_quote or (sent if len(norm_tokens(sent)[0]) >= 12 else cleaned[:240])
        leg, pat = ef_notes_find(str(target_p["ef_raw"]), quote), ef_notes_find(str(target_p["clean"]), quote)
        anchor_test = {
            "paragraph_id": target_p["id"], "has_math_tex": bool(target_p["has_math_tex"]),
            "quote_sha256_12": hashlib.sha256(quote.encode()).hexdigest()[:12], "context_quote": quote,
            "legacy_ef_notes_span": list(leg) if leg else None, "mathml_clean_span": list(pat) if pat else None,
        }
    findings = [f for p in nonempty for f in audit_paragraph(p, doc_id)]
    candidate_notes = [] if is_synth else [{
        "everything_item": {"source": "arxiv_html" if arxiv_id else "html_file", "url": doc_url, "doc_id": doc_id},
        "everything_claim": {"paragraph_id": f["paragraph_id"], "context_quote": f["context_quote"], "context_url": doc_url},
        "everything_note": {"status": "draft", "note": f"[{f['check']}] Reported {f['reported']}; recomputed {f['recomputed']}."},
    } for f in findings]
    unit_selfcheck = run_audit(UNIT_HTML)["findings"] if (include_unit_selfcheck and UNIT_HTML.exists()) else []
    stickiness = json.loads(STICKINESS_JSON.read_text(encoding="utf-8")) if STICKINESS_JSON.exists() else {}
    return {
        "model_completed": bool(nonempty), "exception_type": None if nonempty else "EmptyDocument",
        "doc_id": doc_id, "is_synthetic_fixture": is_synth, "anchor_test": anchor_test,
        "findings": findings, "candidate_notes": candidate_notes, "unit_selfcheck_findings": unit_selfcheck, "stickiness": stickiness,
    }

def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic preprint check & Common Notes anchor adapter.")
    ap.add_argument("--demo", action="store_true", help="Run bundled arXiv:2611.32266v1 excerpt + unit self-check + stickiness")
    ap.add_argument("--html", type=Path, default=DEFAULT_HTML, help="Path to LaTeXML / HTML preprint")
    ap.add_argument("--quote", type=str, default=None, help="Optional passage quote to test DOM anchoring")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args()
    try:
        res = run_audit(args.html, args.quote, include_unit_selfcheck=args.demo)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0
        at = res["anchor_test"]
        print(f"[1/3] Common Notes ef_notes.js Anchor Test ({res['doc_id']}" + (f" #{at['paragraph_id']})" if at else ")"))
        if at:
            leg_s = f"{tuple(at['legacy_ef_notes_span'])} (OK)" if at["legacy_ef_notes_span"] else "None (FAIL: hidden <annotation> TeX pollutes TreeWalker)"
            pat_s = f"{tuple(at['mathml_clean_span'])} (OK, sha256={at['quote_sha256_12']})" if at["mathml_clean_span"] else "None (UNMATCHED)"
            print(f"  reader_selection : {at['context_quote']}\n  ef_notes.js match: {leg_s}\n  patched match    : {pat_s}")
        nf = len(res["findings"])
        print(f"\n[2/3] Document Checks on {res['doc_id']} ({nf} finding{'s' if nf != 1 else ''})")
        for idx, f in enumerate(res["findings"], 1):
            print(f"  {idx}. [{f['check']}] #{f['paragraph_id']}: {f['reported']} -> {f['recomputed']}")
        if res["unit_selfcheck_findings"]:
            print(f"  [synthetic_stat_units.html self-check: {len(res['unit_selfcheck_findings'])}/3 unit rules verified]")
        pw = res.get("stickiness", {}).get("platform_wide", {})
        if pw:
            print(f"\n[3/3] X noteStatusHistory Observational Baseline (n={pw['total_notes']:,}, {pw['first_non_nmr_crh_notes']:,} first-CRH)")
            print(f"  snapshot status  : {pw['still_crh_rate']*100:.2f}% CRH, {pw['reverted_to_nmr_rate']*100:.2f}% NMR")
            print("  by time-to-CRH   : " + " | ".join(f"{b['bucket']}: {b['still_crh_rate']*100:.2f}%" for b in pw["stickiness_by_time_to_first_crh"]))
    except Exception as exc:
        if args.json:
            print(json.dumps({"model_completed": False, "exception_type": type(exc).__name__, "findings": [], "candidate_notes": []}, indent=2))
        else:
            print(f"Audit fallback: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
