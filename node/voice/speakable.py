"""Clinical English made sayable, in place of Ming's own text normaliser.

Ming's FST normaliser is built for Chinese and mangles English (28 Oct 2026): "There's" became
"T here is" (and the line failed to render), "42" became "forty，two" with a Chinese comma (heard as
"40, 2"), "160/95" became "one hundred and sixty ninety，fifth", "3rd" became "three". So the voice
turns it off and does this instead: numbers, doses, units, blood pressures, ordinals, times and
ranges into words, and nothing else. Contractions and ordinary words are left exactly as written.
No hyphens in the output: Ming reads hyphenated words syllable by syllable.
"""
import re

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}


def words(n):
    """A whole number in British English words, without hyphens: 72 -> 'seventy two', 160 -> 'one hundred and sixty'."""
    n = int(n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + words(rest))
    if n < 1_000_000:
        rest = n % 1000
        return words(n // 1000) + " thousand" + ("" if rest == 0 else (" and " if rest < 100 else " ") + words(rest))
    return " ".join(ONES[int(d)] for d in str(n))


def year(n):
    n = int(n)
    if 2000 <= n < 2010:
        return words(n)
    return words(n // 100) + " " + ("hundred" if n % 100 == 0 else ("oh " + ONES[n % 100] if n % 100 < 10 else words(n % 100)))


def ordinal(n):
    w = words(n).split(" ")
    last = w[-1]
    w[-1] = ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return " ".join(w)


def number(s):
    """'2.5' -> 'two point five'; '0.5' -> 'nought point five'; '1,000' -> 'one thousand'."""
    s = s.replace(",", "")
    if "." in s:
        a, b = s.split(".", 1)
        return ("nought" if a in ("", "0") else words(a)) + " point " + " ".join(ONES[int(d)] for d in b)
    return words(s)


UNITS = [  # (pattern after a number, spoken singular, spoken plural)
    (r"mmol/l", "millimole per litre", "millimoles per litre"), (r"micromol/l|µmol/l|umol/l", "micromole per litre", "micromoles per litre"),
    (r"mg/kg", "milligram per kilogram", "milligrams per kilogram"), (r"mg/dl", "milligram per decilitre", "milligrams per decilitre"),
    (r"g/l", "gram per litre", "grams per litre"), (r"ml/min", "millilitre a minute", "millilitres a minute"), (r"ml/hr|ml/h", "millilitre an hour", "millilitres an hour"),
    (r"mmhg", "millimetre of mercury", "millimetres of mercury"), (r"bpm", "beat a minute", "beats a minute"), (r"/min", "a minute", "a minute"),
    (r"mcg|µg|micrograms?", "microgram", "micrograms"), (r"mg|milligrams?", "milligram", "milligrams"), (r"kg|kilos?", "kilogram", "kilograms"),
    (r"ml|mls", "millilitre", "millilitres"), (r"l|litres?", "litre", "litres"), (r"g|grams?", "gram", "grams"), (r"cm", "centimetre", "centimetres"),
    (r"mm", "millimetre", "millimetres"), (r"%", "per cent", "per cent"), (r"°c|degrees", "degree", "degrees"), (r"units?", "unit", "units"),
    (r"mins?|minutes?", "minute", "minutes"), (r"hrs?|hours?", "hour", "hours"),
]
UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(" + "|".join(u[0] for u in UNITS) + r")(?![a-z])", re.I)


def unit(m):
    n, u = m.group(1), m.group(2).lower()
    for pat, one, many in UNITS:
        if re.fullmatch(pat, u, re.I):
            return f"{number(n)} {one if n in ('1', '1.0') else many}"
    return m.group(0)


def speakable(text):
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)                              # 1,000 -> 1000
    t = re.sub(r"\bHbA1c\b", "H B A one C", t, flags=re.I)
    t = re.sub(r"\b(\d{2,3})\s?/\s?(\d{2,3})\b(?!\s?(?:mg|ml|g)\b)", lambda m: f"{words(m.group(1))} over {words(m.group(2))}", t)      # 160/95
    t = re.sub(r"\b(\d{1,2})[:.](\d{2})\s?(am|pm)\b", lambda m: f"{words(m.group(1))} {words(m.group(2)) if m.group(2) != '00' else ''} {' '.join(m.group(3).upper())}", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2})\s?(am|pm)\b", lambda m: f"{words(m.group(1))} {' '.join(m.group(2).upper())}", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: f"{words(m.group(1))} {'o clock' if m.group(2) == '00' else ('oh ' + ONES[int(m.group(2))] if int(m.group(2)) < 10 else words(m.group(2)))}", t)
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal(m.group(1)), t, flags=re.I)
    t = UNIT_RE.sub(unit, t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s?(?:-|–|to)\s?(\d+(?:\.\d+)?)\b", lambda m: f"{number(m.group(1))} to {number(m.group(2))}", t)       # 7-10
    t = re.sub(r"\b(1[1-9]\d\d|20\d\d)\b", lambda m: year(m.group(1)), t)                                                                 # 1990, 2026
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\b", lambda m: number(m.group(0)), t)
    t = re.sub(r"(?<=\w)-(?=\w)", " ", t)                                     # no hyphens: Ming spells them out
    return re.sub(r"\s+", " ", t).strip()


if __name__ == "__main__":
    for s in ["There's some cachexia.", "She's 72 and lives alone.", "Your eGFR is 42 and your HbA1c is 58.", "Your BP is 160/95.",
              "Take 2.5 mg of ramipril and 5mg of bisoprolol.", "Your potassium is 6.2 mmol/L.", "He's had it since the 3rd of March.",
              "Take it at 8am and 8pm.", "I'd say it's 7 out of 10, maybe 7-8.", "Born in 1954, she had a stroke in 2019.", "Heart rate 110 bpm, sats 94%, temp 38.5°C.",
              "Give 1,000 ml over 8 hours.", "It's a 21st birthday.", "co-amoxiclav 625 mg three times a day", "Where's the pain?"]:
        print(f"{s!r:58} -> {speakable(s)}")
