"""Editable lists used by the UI. Add/remove karigar names here only."""

KARIGAR_NONE = "-- none --"

KARIGAR_NAMES = [
    "INHOUSE KARIGAR",
    "IQRA ARTS",
    "Daiyan Mallik",
    "Momtaj Ali Mirja",
    "Nasim Ali Shaikh",
    "BAZAAR KONNECTIONS",
    "BHOLA PARAMANIK",
    "EKHLALKH KHURSHID KHAN",
    "GP EXPORTS",
    "Iqbal Rashid Khan",
    "KADIR HAND EMBROIDERY ARTS",
    "KHAN EMBROIDERY",
    "MIR JAHAN SHAIKH JHARIWALA",
    "Mohd. Qamrul Ansari",
    "NVisage Design Studio",
    "SHAHID MIRZA",
    "SHANU MIRZA",
    "SHYAMU MATA PRASAD PATWA",
    "SK LATIF ALI",
    "YEASMIN BEGAM",
    "AMAR KUMAR PATWA",
    "Mushtaque shaikh",
    "Chetan",
    "Afzaal Rashid Khan",
    "H B Jariwala",
    "Raj patwa arts",
    "Merchandiser",
    "QC Team",
]


def normalize_karigar(value) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "-- none --", KARIGAR_NONE.lower()}:
        return None
    lookup = {name.lower(): name for name in KARIGAR_NAMES}
    return lookup.get(text.lower(), text)


def karigar_select_options() -> list[str]:
    return [KARIGAR_NONE, *KARIGAR_NAMES]
