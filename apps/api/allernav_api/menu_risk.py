from __future__ import annotations

from .models import AllergyTag, MenuItem, PlaceMenu
from .risk_engine import ALLERGEN_TERMS, term_matches


PREPARATION_RISK_TERMS = (
    "sauce",
    "marinade",
    "marinated",
    "garnish",
    "fried",
    "fryer",
    "crispy",
    "curry",
    "dressing",
    "chutney",
    "glaze",
    "shared",
    "aioli",
    "pesto",
    "gravy",
    "shared prep",
    "shared preparation",
)

FISH_PREPARATION_CONTEXT_TERMS = (
    "ramen",
    "broth",
    "soup",
    "soup base",
    "tare",
)

FISH_SAUCE_TERMS = ("sauce",)
FISH_MARINADE_TERMS = ("marinade", "marinated")

FISH_SIMPLE_ITEM_TERMS = (
    "egg",
    "rice",
    "pudding",
    "dessert",
    "ice cream",
    "sorbet",
    "vegetable",
    "greens",
    "mushroom",
    "corn",
)

MENU_ALLERGEN_ALIASES: dict[AllergyTag, tuple[str, ...]] = {
    AllergyTag.FISH: (
        "tilapia",
        "trout",
        "haddock",
        "snapper",
        "mahi",
        "bonito",
        "sardine",
        "dashi",
        "fish sauce",
        "seafood broth",
        "seafood stock",
    ),
    AllergyTag.SHELLFISH: ("seafood", "clam", "mussel", "oyster", "crawfish", "crayfish"),
}

FISH_SHELLFISH_CONTEXT_TERMS = (
    "seafood",
    "oyster",
    "mussel",
    "shrimp",
    "crab",
    "lobster",
    "clam",
    "scallop",
)

NON_FOOD_CATEGORY_NAMES = {
    "add on",
    "add-ons",
    "add ons",
    "addons",
    "dine-in menu",
    "dine in menu",
    "extracted menu",
    "kids menu",
    "menu",
    "menu category",
    "menu categories",
    "recommended toppings set",
    "recommended toppings",
    "topping",
    "toppings",
}

VAGUE_ITEM_NAMES = {
    "special",
    "house special",
    "chef special",
    "combo",
    "combination",
    "lunch",
    "dinner",
    "entrees",
    "mains",
    "appetizers",
    "sides",
    "menu",
}


def is_non_food_category(item: MenuItem) -> bool:
    normalized_name = " ".join(item.name.lower().split()).strip(" :-")
    if normalized_name in NON_FOOD_CATEGORY_NAMES:
        return True
    if item.price or (item.description or "").strip():
        return False
    return (
        normalized_name.endswith(" menu")
        or normalized_name.endswith(" section")
        or normalized_name.endswith(" category")
        or normalized_name in VAGUE_ITEM_NAMES
    )


def classify_menu_item(
    item: MenuItem,
    selected_allergens: list[AllergyTag],
    *,
    source_confidence: float | None = None,
    section_title: str | None = None,
) -> MenuItem:
    if not selected_allergens:
        return item.model_copy(
            update={
                "risk_label": None,
                "matched_allergens": [],
                "risk_reasons": [],
                "verification_question": None,
            }
        )

    name = item.name.strip()
    description = (item.description or "").strip()
    text = f"{name} {description}".strip().lower()
    allergen_text = f"{text} {section_title or ''}".strip().lower()
    matched = sorted(
        {
            allergen
            for allergen in selected_allergens
            if allergen in item.confirmed_allergens
            or any(term_matches(allergen_text, term) for term in ALLERGEN_TERMS[allergen])
            or any(term_matches(allergen_text, term) for term in MENU_ALLERGEN_ALIASES.get(allergen, ()))
        },
        key=lambda allergen: allergen.value,
    )
    inferred_matches = sorted(
        set(selected_allergens).intersection(item.inferred_risks),
        key=lambda allergen: allergen.value,
    )
    source_quality = item.ocr_confidence if item.ocr_confidence is not None else source_confidence
    source_quality = source_quality if source_quality is not None else 0.72
    selected_text = ", ".join(allergen.value.replace("_", " ") for allergen in selected_allergens)
    selected_text = selected_text or "the selected allergens"

    if matched:
        labels = ", ".join(allergen.value.replace("_", " ") for allergen in matched)
        return item.model_copy(
            update={
                "risk_label": "avoid",
                "matched_allergens": matched,
                "risk_reasons": [f"Menu text or structured evidence identifies selected allergen: {labels}."],
                "verification_question": f"Can you confirm whether {name} contains {labels} in any ingredient or garnish?",
                "confidence": round(min(0.98, max(0.78, source_quality + 0.12)), 2),
            }
        )

    if item.allergen_codes:
        labels = ", ".join(allergen.value.replace("_", " ") for allergen in selected_allergens)
        return item.model_copy(
            update={
                "risk_label": "possible_lower_risk",
                "matched_allergens": [],
                "risk_reasons": [f"The menu allergen key does not list the selected allergens ({labels})."],
                "verification_question": f"Can you confirm the allergen key for {name} is current and includes sauces and preparation?",
                "confidence": round(min(0.94, max(0.72, source_quality + 0.08)), 2),
            }
        )

    if inferred_matches:
        labels = ", ".join(allergen.value.replace("_", " ") for allergen in inferred_matches)
        return item.model_copy(
            update={
                "risk_label": "needs_check",
                "matched_allergens": [],
                "risk_reasons": [f"Extraction flagged possible {labels}, but direct menu evidence is missing."],
                "verification_question": f"Can you confirm whether {name} contains {labels} in any ingredient or preparation step?",
                "confidence": round(min(0.78, max(0.48, source_quality - 0.08)), 2),
            }
        )

    selected_set = set(selected_allergens)
    if selected_set == {AllergyTag.FISH}:
        shellfish_terms = [term for term in FISH_SHELLFISH_CONTEXT_TERMS if term_matches(allergen_text, term)]
        fish_context_terms = [term for term in FISH_PREPARATION_CONTEXT_TERMS if term_matches(text, term)]
        simple_fish_item = any(term_matches(text, term) for term in FISH_SIMPLE_ITEM_TERMS)
        sauce_terms = [term for term in FISH_SAUCE_TERMS if term_matches(text, term)]
        marinade_terms = [term for term in FISH_MARINADE_TERMS if term_matches(text, term)]
        preparation_terms = shellfish_terms + fish_context_terms + marinade_terms + ([] if simple_fish_item else sauce_terms)
    else:
        preparation_terms = [term for term in PREPARATION_RISK_TERMS if term_matches(text, term)]
    if preparation_terms:
        term_text = ", ".join(preparation_terms[:3])
        confidence = min(0.82, max(0.5, source_quality - 0.05 + min(len(description.split()), 8) * 0.015))
        return item.model_copy(
            update={
                "risk_label": "needs_check",
                "matched_allergens": [],
                "risk_reasons": [
                    "Seafood, broth, or sauce may need staff verification."
                    if selected_set == {AllergyTag.FISH}
                    else f"Preparation wording needs staff verification: {term_text}."
                ],
                "verification_question": (
                    f"Does the {term_text} used for {name} contain {selected_text}, or share preparation equipment?"
                ),
                "confidence": round(confidence, 2),
            }
        )

    description_words = [word for word in description.split() if any(character.isalpha() for character in word)]
    normalized_name = " ".join(name.lower().split())
    raw_name_parts = normalized_name.split()
    name_words = [word for word in normalized_name.split() if any(character.isalpha() for character in word)]
    vague_name = (
        normalized_name in VAGUE_ITEM_NAMES
        or normalized_name.startswith("choose ")
        or normalized_name.endswith(" options")
        or (len(name_words) <= 1 and normalized_name in {"dish", "item", "plate"})
        or (
            len(raw_name_parts) == 2
            and raw_name_parts[0] in {"dish", "item", "plate"}
            and raw_name_parts[1].isdigit()
        )
    )
    if vague_name:
        return item.model_copy(
            update={
                "risk_label": "insufficient_info",
                "matched_allergens": [],
                "risk_reasons": ["The menu does not provide enough ingredient or preparation detail."],
                "verification_question": f"What ingredients and preparation steps are used for {name}?",
                "confidence": round(min(0.48, max(0.22, source_quality - 0.35)), 2),
            }
        )

    context_score = min(0.14, len(description_words) * 0.012)
    name_context = min(0.08, len(name_words) * 0.02)
    return item.model_copy(
        update={
            "risk_label": "possible_lower_risk",
            "matched_allergens": [],
            "risk_reasons": ["No selected allergen terms were found in the available menu text."],
            "verification_question": (
                f"Can you verify that {name} contains no {selected_text} and is prepared without shared-contact risk?"
            ),
            "confidence": round(min(0.9, max(0.56, source_quality - 0.08 + context_score + name_context)), 2),
        }
    )


def classify_place_menu(menu: PlaceMenu, selected_allergens: list[AllergyTag]) -> PlaceMenu:
    return menu.model_copy(
        update={
            "sections": [
                section.model_copy(
                    update={
                        "items": [
                            (
                                classify_menu_item(
                                    item,
                                    selected_allergens,
                                    source_confidence=menu.extraction_confidence,
                                    section_title=section.title,
                                )
                                if selected_allergens
                                else item
                            )
                            for item in section.items
                            if not is_non_food_category(item)
                        ]
                    }
                )
                for section in menu.sections
                if any(not is_non_food_category(item) for item in section.items)
            ]
        }
    )
