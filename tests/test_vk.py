import time
from types import SimpleNamespace

from storage import Store
from vk_client import VKError
from vk_parser import _execute_batch, _normalize_group_ref, collect_leads


class Client:
    def __init__(self, posts):
        self.posts = posts
        self.calls = []
        self.fail_offset = None

    def get_api(self):
        return SimpleNamespace(
            groups=SimpleNamespace(getById=lambda **kwargs: {"groups": [{"id": 12, "name": "Соседи"}]})
        )

    def method(self, method, params, raw=False):
        import json

        if method == "execute":
            code = params["code"]
            values = json.loads(code[code.index("(") + 1 : code.rindex(")")])
            self.calls.append(values["offset"])
            if values["offset"] == self.fail_offset:
                return {"response": [False], "execute_errors": [{"error_code": 7}]}
            return {
                "response": [{"items": self.posts[values["offset"] : values["offset"] + values["count"]]}]
            }
        raise AssertionError(method)


def posts(number):
    return [
        {
            "id": number - index,
            "owner_id": -12,
            "text": "Нужен ремонт" if index == 150 else "Новости",
            "date": int(time.time()),
        }
        for index in range(number)
    ]


def test_deep_page_and_budget_resume_with_new_front(cfg):
    cfg["vk"].update(enabled=True, group_ids=[12], posts_per_run=100, max_pages_per_query=1)
    client = Client(posts(250))
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        assert not collect_leads(cfg, store, reports, client)
        assert not reports[0].complete and store.cursor("vk:12")["offset"] == 100
        client.posts.insert(0, {"id": 251, "owner_id": -12, "text": "Новости", "date": int(time.time())})
        reports = []
        assert len(collect_leads(cfg, store, reports, client)) == 1
        assert store.cursor("vk:12")["offset"] == 201
        collect_leads(cfg, store, [], client)
        assert "watermark" in store.cursor("vk:12")
        assert store.lead("vk:-12_100").source_group_id == "vk:12"


def test_page_failure_preserves_inbox_and_cursor(cfg):
    cfg["vk"].update(enabled=True, group_ids=[12], max_pages_per_query=5)
    items = posts(250)
    items[0]["text"] = "Нужен ремонт"
    client = Client(items)
    client.fail_offset = 100
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        leads = collect_leads(cfg, store, reports, client)
        assert leads and store.lead(leads[0].dedupe_key())
        assert store.cursor("vk:12")["offset"] == 100
        assert reports[0].errors == ["VK:7"]


def test_old_pinned_does_not_stop_recent_pages(cfg):
    cfg["vk"].update(enabled=True, group_ids=[12], max_pages_per_query=5)
    items = posts(250)
    items.insert(0, {"id": 1, "owner_id": -12, "text": "Нужен ремонт", "date": 1, "is_pinned": 1})
    assert len(collect_leads(cfg, client=Client(items))) == 1


def test_execute_retries_only_transient_failed_subcalls(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *args: None)

    class RawClient:
        def __init__(self):
            self.calls = []

        def method(self, method, params, raw=False):
            self.calls.append(params["code"])
            assert raw is True
            if len(self.calls) == 1:
                return {
                    "response": [{"items": [1]}, False, False],
                    "execute_errors": [{"error_code": 6}, {"error_code": 1051}],
                }
            return {"response": [{"items": [2]}]}

    client = RawClient()
    values = _execute_batch(client, [("wall.get", {"owner_id": index}) for index in [1, 2, 3]])
    assert values[:2] == [{"items": [1]}, {"items": [2]}]
    assert isinstance(values[2], VKError) and values[2].code == 1051
    assert (
        len(client.calls) == 2
        and '"owner_id": 2' in client.calls[1]
        and '"owner_id": 1' not in client.calls[1]
    )


def test_canonical_refs():
    assert [_normalize_group_ref(ref) for ref in [-12, "club12", "public12", "https://vk.ru/club12?x=1"]] == [
        "12"
    ] * 4
    assert _normalize_group_ref("https://vk.com/Some_Group") == "some_group"


def test_global_search_uses_next_from_and_preserves_budget_cursor(cfg):
    cfg["general"]["keywords"] = ["нужен ремонт"]
    cfg["vk"].update(enabled=True, group_ids=[], use_global_newsfeed_search=True, max_pages_per_query=1)

    class SearchClient:
        def __init__(self):
            self.calls = []

        def method(self, method, params, raw=False):
            import json

            values = json.loads(params["code"][params["code"].index("(") + 1 : params["code"].rindex(")")])
            self.calls.append(values)
            item = {"id": len(self.calls), "owner_id": -12, "text": "Нужен ремонт", "date": int(time.time())}
            return {
                "response": [{"items": [item], "next_from": "next-page" if len(self.calls) == 1 else None}]
            }

    client = SearchClient()
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        assert collect_leads(cfg, store, reports, client)
        assert not reports[0].complete and store.cursor("vk:search:нужен ремонт")["next_from"] == "next-page"
        assert collect_leads(cfg, store, [], client)
        assert client.calls[1]["start_from"] == "next-page" and store.cursor("vk:search:нужен ремонт") == {}


def test_response_size_splits_then_direct_fallback(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *args: None)

    class BigClient:
        def method(self, method, params, raw=False):
            if method == "execute":
                raise VKError(13)
            return {"items": [params["owner_id"]]}

    assert _execute_batch(BigClient(), [("wall.get", {"owner_id": 1}), ("wall.get", {"owner_id": 2})]) == [
        {"items": [1]},
        {"items": [2]},
    ]
