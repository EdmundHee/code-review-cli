from ghcr.models import (
    Finding,
    LensResult,
    CoverageVerdict,
    Usage,
    merge_usages,
)


def test_merge_usages_sums_all_fields():
    out = merge_usages([
        Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        Usage(prompt_tokens=20, completion_tokens=2, total_tokens=22),
        Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    ])
    assert out == Usage(prompt_tokens=35, completion_tokens=6, total_tokens=41)


def test_merge_usages_empty_is_zero():
    assert merge_usages([]) == Usage()


def test_finding_defaults():
    f = Finding(severity="WARNING", file="a.py")
    assert f.confidence is None and f.id == 0 and f.lens == ""


def test_lens_result_carries_coverage_optional():
    lr = LensResult(lens="security", findings=(), usage=Usage(), ok=True)
    assert lr.coverage is None
    cov = CoverageVerdict(has_tests=False, detail="no test for new fn foo")
    lr2 = LensResult(lens="test_coverage", findings=(), usage=Usage(), ok=True, coverage=cov)
    assert lr2.coverage.has_tests is False
