from app.core.ids import WORKLOAD
from app.core.ids import new_uid
from app.modules.deployments.naming import MAX_LENGTH
from app.modules.deployments.naming import object_name


def test_two_workloads_with_one_name_get_two_object_names():
    a, b = new_uid(WORKLOAD), new_uid(WORKLOAD)
    assert object_name("qwen", a) != object_name("qwen", b)
    assert object_name("qwen", a) == f"qwen-{a[-8:]}"


def test_an_object_name_is_a_short_rfc_1123_label():
    uid = new_uid(WORKLOAD)
    for name in ("Qwen 3.8 / 27B-Instruct", "x" * 200, "---", ""):
        rendered = object_name(name, uid)
        assert len(rendered) <= MAX_LENGTH
        assert rendered.replace("-", "").isalnum() and rendered == rendered.lower()
        assert not rendered.startswith("-") and not rendered.endswith("-")
