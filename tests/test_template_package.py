"""Contract, content-preservation and rendering regressions for SVG packages."""

import base64
import io

import pytest
from lxml import etree
from PIL import Image
from pydantic import ValidationError

from landppt.services.slide.package_generation.candidate_filter import (
    compatible_components,
    incompatibilities,
)
from landppt.services.slide.package_generation.renderer import (
    PackageRenderError,
    render_page,
)
from landppt.services.slide.svg_page.sanitize import parse_svg, serialize
from landppt.services.slide.svg_page.shell import is_svg_page_html
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.schemas import (
    ContentBlock,
    PageContent,
    Slot,
    TemplatePackage,
)
from landppt.services.template_package.service import validate_package
from landppt.services.template_package.validator import (
    VALIDATION_IMAGE,
    PackageValidationError,
    validate_component,
)


@pytest.fixture
def package():
    return load_builtin_package()


def replace(model, **updates):
    return type(model).model_validate({**model.model_dump(), **updates})


def component(package, component_id):
    return next(item for item in package.components if item.id == component_id)


def test_package_round_trip_is_immutable_and_hash_tracks_actual_content(package):
    restored = TemplatePackage.model_validate_json(package.model_dump_json())
    assert restored == package
    assert restored.content_hash() == package.content_hash()
    assert replace(package, name="不同名称").content_hash() != package.content_hash()
    assert replace(package, version=2).content_hash() != package.content_hash()
    with pytest.raises(ValidationError):
        package.components[0].examples[0].title = "mutation"


def test_builtin_package_passes_all_example_gates(package):
    report = validate_package(package)
    assert report["components"] == report["examples_rendered"] == 10
    assert len({item.family for item in package.components}) == 8


def test_package_validation_uses_a_decodable_raster_fixture():
    image = Image.open(io.BytesIO(base64.b64decode(VALIDATION_IMAGE.split(",", 1)[1])))
    image.verify()


@pytest.mark.parametrize(
    "component_id",
    [
        "cover",
        "section",
        "points_3",
        "comparison_2",
        "process_3",
        "process_4",
        "process_5",
        "image_1",
        "metrics_3",
        "summary_3",
    ],
)
def test_every_builtin_renders_without_changing_content_or_package(
    package, component_id
):
    item = component(package, component_id)
    content = item.examples[0]
    original = package.model_dump_json()
    page = render_page(
        package,
        component_id,
        content,
        assets={v.id: VALIDATION_IMAGE for v in content.visual_briefs},
        allowed_image_urls=frozenset({VALIDATION_IMAGE}),
    )
    assert is_svg_page_html(page.html_content)
    assert page.metadata["layout_report"]["blocking"] == []
    assert page.metadata["content_revision"] == content.content_revision
    assert page.metadata["package_hash"] == package.content_hash()
    assert package.model_dump_json() == original
    text = "".join(parse_svg(page.svg).itertext())
    assert content.title in text
    for block in content.blocks:
        assert block.heading in text
        assert block.body in text


def test_xml_binding_escapes_markup_instead_of_executing_it(package):
    content = PageContent(slide_id="safe", title='<script> & "内容"')
    rendered = render_page(package, "cover", content)
    root = parse_svg(rendered.svg)
    assert content.title in "".join(root.itertext())
    assert "<script>" not in rendered.html_content


def test_extra_blocks_and_metrics_are_not_silently_lost(package):
    points = component(package, "points_3")
    content = replace(
        points.examples[0],
        blocks=(*points.examples[0].blocks, ContentBlock(id="extra", body="必须保留")),
    )
    reasons = incompatibilities(points, content)
    assert any("unbound content: blocks[3].body" in reason for reason in reasons)
    assert not compatible_components(package, content, locked_component_id="points_3")
    with pytest.raises(PackageRenderError, match="unbound content"):
        render_page(package, "points_3", content)
    with pytest.raises(PackageRenderError, match="unbound content"):
        render_page(package, "cover", component(package, "metrics_3").examples[0])


def test_required_content_and_locked_components_are_hard_constraints(package):
    image = component(package, "image_1")
    content = replace(image.examples[0], visual_briefs=())
    assert any(
        "missing required content" in reason
        for reason in incompatibilities(image, content)
    )
    assert (
        compatible_components(
            package,
            component(package, "cover").examples[0],
            locked_component_id="does_not_exist",
        )
        == ()
    )


def test_image_preferences_and_remaining_budget_filter_candidates(package):
    content = component(package, "image_1").examples[0]
    assert compatible_components(package, content, allow_images=False) == ()
    assert compatible_components(package, content, image_budget=0) == ()
    assert [
        item.id for item in compatible_components(package, content, image_budget=1)
    ] == ["image_1"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "//evil.test/a.png",
        "data:image/svg+xml;base64,abc",
        "/../secret.png",
        "/%2e%2e/secret.png",
        "https://[invalid",
        "https://user:secret@example.com/a.png",
    ],
)
def test_even_allowlisted_unsafe_asset_schemes_are_rejected(package, url):
    content = component(package, "image_1").examples[0]
    with pytest.raises(PackageRenderError, match="unapproved image"):
        render_page(
            package,
            "image_1",
            content,
            assets={"visual1": url},
            allowed_image_urls=frozenset({url}),
        )


def test_images_require_explicit_resolver_approval(package):
    content = component(package, "image_1").examples[0]
    with pytest.raises(PackageRenderError, match="unapproved image"):
        render_page(
            package,
            "image_1",
            content,
            assets={"visual1": "https://example.com/image.png"},
        )
    with pytest.raises(PackageRenderError, match="unapproved image"):
        render_page(package, "image_1", content)


def test_approved_image_binding_preserves_crop_and_metadata(package):
    content = component(package, "image_1").examples[0]
    url = "/uploads/approved.png"
    rendered = render_page(
        package,
        "image_1",
        content,
        assets={"visual1": url},
        allowed_image_urls=frozenset({url}),
    )
    assert 'href="/uploads/approved.png"' in rendered.svg
    assert 'preserveAspectRatio="xMidYMid slice"' in rendered.svg
    assert rendered.metadata["assets"] == {"visual1": url}


@pytest.mark.parametrize(
    "bad_svg",
    [
        "<script>alert(1)</script>",
        '<rect width="10" height="10" onclick="alert(1)"/>',
        "<foreignObject><div>hidden content</div></foreignObject>",
        '<image id="extra" href="https://evil.test/x.png" width="10" height="10"/>',
        '<text id="slot_0" x="1" y="1">duplicate</text>',
    ],
)
def test_unsafe_or_ambiguous_components_are_rejected(package, bad_svg):
    item = component(package, "cover")
    modified = replace(item, svg=item.svg.replace("</svg>", bad_svg + "</svg>"))
    with pytest.raises(PackageValidationError):
        validate_component(modified)


@pytest.mark.parametrize("replacement", ['x="nan"', 'x="-1"', 'x="1250"'])
def test_invalid_slot_geometry_is_rejected(package, replacement):
    item = component(package, "cover")
    modified = replace(
        item, svg=item.svg.replace('id="slot_0" x="80"', f'id="slot_0" {replacement}')
    )
    with pytest.raises(PackageValidationError):
        validate_component(modified)


def test_slot_in_hidden_container_is_rejected(package):
    item = component(package, "cover")
    root = parse_svg(item.svg)
    node = root.xpath('//*[@id="slot_0"]')[0]
    group = etree.SubElement(root, "{http://www.w3.org/2000/svg}g", opacity="0")
    group.append(node)
    modified = replace(item, svg=serialize(root))
    with pytest.raises(PackageValidationError, match="opaque"):
        validate_component(modified)


def test_long_content_is_rejected_without_truncation(package):
    content = PageContent(slide_id="long", title="长" * 100)
    with pytest.raises(PackageRenderError, match="capacity exceeded"):
        render_page(package, "cover", content)


def test_measured_overflow_rejected_even_when_character_limit_allows_it(package):
    item = component(package, "cover")
    root = parse_svg(item.svg)
    node = root.xpath('//*[@id="slot_1"]')[0]
    node.set("data-box-w", "12")
    modified = replace(item, svg=serialize(root))
    modified_package = replace(package, components=(modified,))
    with pytest.raises(PackageRenderError, match="layout:|excessive font reduction"):
        render_page(
            modified_package,
            "cover",
            PageContent(slide_id="wide", title="这个标题不能装进一个很窄的文本框"),
        )


def test_reference_integrity_and_unknown_fields_are_enforced():
    with pytest.raises(ValidationError, match="source_refs"):
        PageContent(
            slide_id="s",
            title="指标",
            metrics=(
                {"id": "m", "label": "人数", "value": "10", "source_refs": ["missing"]},
            ),
        )
    with pytest.raises(ValidationError):
        PageContent(slide_id="s", title="内容", chart_data={"values": [1, 2]})
    with pytest.raises(ValidationError, match="unique"):
        PageContent(
            slide_id="s",
            title="内容",
            blocks=(ContentBlock(id="b", body="a"), ContentBlock(id="b", body="b")),
        )


@pytest.mark.parametrize(
    "field",
    [
        "__class__",
        "blocks[-1].body",
        "blocks[01].body",
        "blocks[0].source_refs",
        "metrics[0].value.upper()",
    ],
)
def test_arbitrary_field_paths_are_rejected(field):
    with pytest.raises(ValidationError):
        Slot(node_id="n", field=field)


def test_duplicate_slot_binding_and_wrong_slot_kind_are_rejected(package):
    item = component(package, "cover")
    with pytest.raises(ValidationError):
        replace(item, slots=(*item.slots, item.slots[0]))
    with pytest.raises(ValidationError):
        Slot(node_id="n", field="title", kind="image")


def test_unknown_component_never_falls_back_to_a_success_placeholder(package):
    with pytest.raises(PackageRenderError, match="unknown component"):
        render_page(package, "missing", PageContent(slide_id="s", title="示例"))
