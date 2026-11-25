"""Project navigation grounded in indexed text and act work descriptions."""

from __future__ import annotations

import re

from bis.services.executive_pipeline import norm

PLACE = re.compile(
    r"\b(?P<kind>участ(?:ок|ка|ке|ку|ком|ки|ках)|захватк[аиуе]|подобъект(?:а|е|у|ом)?|"
    r"корпус(?:а|е|у|ом)?|секци[яиюе]|помещени[еяию])"
    r"\s*(?::\s*(?P<name>[^;|\n,]{1,120})|(?:№\s*)?"
    r"(?P<identifier>(?:[\w-]*\d[\w./-]*|(?-i:[А-ЯЁA-Z]))(?!\w)))",
    re.I,
)
WORK = re.compile(
    r"(?:^|[;|])\s*(?:\d+\.\s*)?(?:(?:к освидетельствованию )?предъявлены следующие )?"
    r"(?:вид(?:ы)? работ|перечень работ|работ[аы])\s*:\s*([^;|]+)",
    re.I,
)
PLANNED_WORK = re.compile(
    r"\bпредусмотрен[оаы]?\s+((?:устройство|монтаж|демонтаж|прокладка|установка|"
    r"бетонирование|армирование|гидроизоляция|окраска|сварка|засыпка)\b[^;|]*)",
    re.I,
)
MORE_PLACES = re.compile(r"\s*(?:,|\bи\b|\bили\b)\s*(?:№\s*)?(?:\d|[А-ЯЁA-Z](?!\w))")
PLACE_TYPES = {
    "участ": ("Участок", False),
    "захват": ("Захватка", False),
    "подобъ": ("Подобъект", True),
    "корпус": ("Корпус", True),
    "секци": ("Секция", True),
    "помещ": ("Помещение", True),
}


def declared_works(text):
    return list(dict.fromkeys(match[1].strip() for line in text.splitlines() for match in WORK.finditer(line)))


def _scope(text):
    areas, subobjects = {}, {}
    for match in PLACE.finditer(text):
        if match["identifier"] and MORE_PLACES.match(text, match.end()):
            return None
        label, subobject = next(value for prefix, value in PLACE_TYPES.items()
                                if match["kind"].lower().startswith(prefix))
        designation = (match["name"] or match["identifier"].upper()).strip(" ./")
        if not designation:
            continue
        name = f'{label} {designation}'
        (subobjects if subobject else areas)[norm(name)] = name
    if len(areas) > 1 or len(subobjects) > 1:
        return None
    area = next(iter(areas.values()), None)
    subobject = next(iter(subobjects.values()), None)
    if area or subobject:
        return (area or subobject, subobject if area else None)
    return None


def _work_name(text):
    match = PLACE.search(text)
    if match:
        text = re.sub(r"(?:\s+в|\s+на)\s*$", "", text[:match.start()].rstrip(" ,;"))
    return text.split(";", 1)[0].strip(" .,;") or None


def work_index(project_chunks, acts):
    areas = {}
    all_work = {}
    unassigned = []

    def section(area, subobject=None):
        parent = areas.setdefault(norm(area), {"label": area, "works": [], "subobjects": []})
        if subobject:
            for child in parent["subobjects"]:
                if norm(child["label"]) == norm(subobject):
                    return child
            child = {"label": subobject, "works": []}
            parent["subobjects"].append(child)
            return child
        return parent

    def add(scope, name, source=None, act_id=None):
        area, subobject = scope
        items = section(area, subobject)["works"]
        if not name:
            return
        key = (norm(area), norm(subobject), norm(name))
        if key not in all_work:
            item = {"name": name, "source": source, "act_ids": []}
            all_work[key] = item
            items.append(item)
        if act_id:
            all_work[key]["act_ids"].append(act_id)

    for document_id, page, content in project_chunks:
        context = _scope(content)
        lines = [(line, _scope(line)) for line in content.splitlines()]
        if context and context[1] and not any(scope == context for _, scope in lines):
            context = None
        for line, scope in lines:
            if scope:
                add(scope, None)
            if scope or (context and not PLACE.search(line)):
                for work in declared_works(line) or [match[1] for match in PLANNED_WORK.finditer(line)]:
                    add(scope or context, _work_name(work), {"document_id": document_id, "page": page})

    for act in acts:
        work = act["works"]
        scope = _scope(work)
        name = _work_name(work)
        if scope and name:
            add(scope, name, act_id=act["id"])
        else:
            unassigned.append(act["id"])

    return {"areas": list(areas.values()), "unassigned_act_ids": unassigned}
