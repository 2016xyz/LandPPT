"""A page shaped for no layout (e.g. blocks + metrics) must not fail forever."""

from landppt.services.slide.package_generation.candidate_filter import (
    demote_metrics,
    has_structural_fit,
)
from landppt.services.slide.package_generation.content_service import (
    strip_model_extras,
)
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.schemas import PageContent


def mixed_page():
    return PageContent.model_validate(
        {
            "slide_id": "p5",
            "title": "贡献流程的收益",
            "source_refs": ["outline"],
            "blocks": [
                {"id": f"b{i}", "heading": f"要点{i}", "body": "说明文字"}
                for i in range(3)
            ],
            "metrics": [
                {
                    "id": f"m{i}",
                    "label": f"指标{i}",
                    "value": str(10 * (i + 1)),
                    "unit": "%",
                    "source_refs": ["outline"],
                }
                for i in range(3)
            ],
        }
    )


def test_blocks_plus_metrics_is_demoted_into_a_fitting_shape():
    package = load_builtin_package()
    page = mixed_page()
    assert not has_structural_fit(package, page, allow_images=False)
    demoted = demote_metrics(page, package, allow_images=False)
    assert demoted is not None and not demoted.metrics
    assert has_structural_fit(package, demoted, allow_images=False)
    bodies = " ".join(b.heading + b.body for b in demoted.blocks)
    for metric in page.metrics:
        assert metric.label in bodies and metric.value + metric.unit in bodies


def test_model_commentary_fields_are_dropped_before_validation():
    raw = mixed_page().model_dump(mode="json")
    raw["issues_fix_log"] = ["shortened"]
    raw["blocks"][0]["note"] = "x"
    page = PageContent.model_validate(strip_model_extras(raw))
    assert page.slide_id == "p5"
