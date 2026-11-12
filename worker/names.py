"""Extra name detection for Lebanese and Arab names.

spaCy's English model knows English names well but misses many Arab ones,
especially short first names ("Jad", "Chadi") and any name in Arabic script.
This module adds:

  * NameListRecognizer: common Lebanese/Arab first names (Latin spellings and
    Arabic script), extended over a following surname such as "Abou Jaoude".
  * propagate_names(): once a name is found anywhere, every other mention of
    it (and of its first or last name alone) is hidden too.
  * person_tag_for(): lets "Rami" share the placeholder of "Rami Haddad".
"""

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

# Common first names, Latin spellings (Lebanese, Levantine, Gulf, Christian and
# Muslim). Words that are also everyday English words are left out on purpose
# (May, Joy, Hope, Grace, Said, Ward, Basil, Ray...) to avoid false hits.
LATIN_FIRST_NAMES = """
Abbas Abdallah Abdullah Abdo Adam Adel Adib Adnan Afif Ahmad Ahmed Akram Alaa Ali Alia Aliaa
Amal Amani Amer Amin Amine Amira Ammar Anas Anis Antoine Antonio Antony Anwar Ashraf Assaad Assad
Ayman Aya Ayah Ayoub Azar Badih Bahaa Bakr Bana Bashar Bashir Bassam Bassel Batoul Bechara Bilal
Boutros Carine Carla Carmen Celine Chadi Chady Chafic Charbel Chantal Chirine Christelle Christiane
Cyrine Dalia Dalal Dana Dani Dany Dima Dina Diala Dory Doha Douaa Ehab Elias Elie Elia Elissa Emad
Eyad Fadi Fady Fadia Fadwa Fahd Faisal Farah Fares Farid Farouk Fatima Fatme Fawaz Fawzi Fayez Fouad
Gaby Gabriel Georges George Ghada Ghassan Ghazi Ghina Ghinwa Habib Hadi Hady Hadeel Haifa Haitham
Hala Hamad Hamza Hanadi Hanan Hani Hania Hassan Hasan Hassane Hatem Haytham Hazem Hiba Hicham Hisham
Houda Huda Hussein Husam Ibrahim Ihab Imad Iman Imane Inaya Issa Issam Jaafar Jad Jamal Jamil Jana
Jawad Jihad Jihane Joelle Joe Joey Johnny Josiane Joumana Jumana Jean Joseph Julien Kamal Kamel
Kareem Karim Karima Karl Katia Khaled Khalil Khalid Lama Lana Layal Layla Leila Lea Lara Lina Loubna
Lynn Maher Mahmoud Majd Majed Malak Malek Manal Mansour Marc Maroun Marwan Marwa Maryam Mariam Maya
Mazen Mehdi Michel Mira Mireille Mohamad Mohammad Mohammed Muhammad Mohamed Mona Mounir Mounira Mourad
Moussa Mustafa Moustafa Nabil Nada Nadia Nadim Nadine Nagham Naim Najib Najwa Nancy Nasser Nawal
Nayla Nazih Nicolas Nidal Nisrine Nizar Noor Nour Noura Nouhad Omar Omran Ossama Osama Paula Pierre
Rabih Rabie Rafic Rafik Rami Ramy Rana Rania Rasha Rawad Rayan Rayane Reem Rim Rima Riad Riyad Rita
Roula Rouba Roy Rudy Saad Saber Sabine Sabah Sahar Saleh Salim Salma Salwa Sam Samer Sami Samia Samir
Samira Sana Sandra Sara Sarah Serge Shadi Sherif Siham Simon Souad Suzanne Tala Talal Tamer Tamara
Tania Tarek Tareq Tarik Toni Tony Wadih Wael Wafa Walid Waleed Wassim Wissam Yara Yasmine Yasmina
Yasser Youssef Yousef Yusuf Zahra Zahraa Zeina Zeinab Ziad Zaher Zaid Zainab Zakaria Zouhair
Alain Albert Alexandre Alexandra Alfred Andre Andrea Angela Anthony Antoinette Arlette Armand Bernard
Camille Carole Caroline Cecile Charles Claudine Danielle Daniel David Denise Dominique Edgar Edmond Edouard
Eliane Elham Emile Emilie Eric Etienne Fabienne Francois Frederic Gisele Hanane Helene Jacques Jacqueline
Jeanine Jessica Joanna Josephine Joyce Jules Karen Laurent Liliane Lucien Marie Marianne Marlene Maurice
Melissa Michele Micheline Mikhael Mouna Myriam Naji Najat Naya Nayef Nelly Nicole Noel Odette Pascale Patrick
Paul Philippe Rachelle Raymond Rene Ricardo Rima Robert Roger Roland Rony Sabrina Samar Sylvie Sophie
Stephanie Therese Valerie Vanessa Victor Viviane Wajdi Wissal Yolla Yvonne Zoe Rawan Raghad Lamia Lamis
Hussam Ghassan Jihan Kinda Loulwa Maha Majida Nayef Nawaf Rakan Sultan Turki Abdulaziz Abdulrahman
Abdulla Faris Hamdan Mansoor Rashid Saeed Saud Waleed Yazan Yazeed Ziyad Lojain Shahd Hessa Mashael Nouf Reem
""".split()

# The same idea in Arabic script.
ARABIC_FIRST_NAMES = """
أحمد احمد محمد محمود مصطفى علي عمر عثمان خالد خليل كريم جاد رامي شادي فادي هادي سامي سامر ماهر مازن
وائل وليد زياد طارق رائد رياض نبيل نادر ناصر نصر حسن حسين حسام هشام هيثم إبراهيم ابراهيم إيلي ايلي جورج
بطرس شربل مارون ميشال انطوان أنطوان جوزيف يوسف موسى عيسى الياس إلياس فؤاد بشير بشارة جميل جمال كمال
أنور انور منير مروان مالك ربيع رفيق سعد سعيد سليم سمير عادل عماد عصام أيمن ايمن بلال حمزة عباس جعفر
فاطمة زينب مريم ليلى ليلي لينا لانا رنا رانيا ريم ريما رولا ربى هبة هيفاء هالة هدى منى نور نورا نادين
ندى نجوى نوال سارة ساره سلمى سلوى سهى سهام سعاد سمر سمية دانا دينا ديما داليا جنى جمانة كارين كارلا
مايا ميرا ميراي جويل ريتا ناتالي تالا يارا ياسمين زينة غادة غنوة شيرين سيرين نسرين حنان إيمان ايمان أمل امل
""".split()

# Particles that sit inside Arab surnames ("Ziad Abou Jaoude", "Rita Bou Khalil").
PARTICLES = {"abou", "abu", "bou", "abi", "abdel", "abdul", "abd", "al", "el", "bin", "ben", "ibn", "bint", "ould", "de"}

# Capitalised words that should never be swallowed as a surname.
NOT_SURNAMES = set("""
The A An And Or But If Then So To From For With Without Of In On At By Is Are Was Were Be Been Will Would
Can Could Should Shall May Might Must Do Does Did Has Have Had Not No Yes Please Thanks Thank Hi Hello Dear
Regards Best Team Client Manager Director Producer Brand Marketing Agency Creative Head Lead Senior Junior
Account Executive Officer President CEO CMO CFO COO Project Campaign Deck Brief Launch Shoot Meeting Call Notes
Email Phone Mobile Tel Fax Cc Bcc Subject Re Fwd Fw Attachment Attached Note Update Urgent Draft Final
Office Studio Group Company Holding Bank Hospital University School Street Road Building Center Centre Monday Tuesday Wednesday Thursday Friday Saturday Sunday
January February March April June July August September October November December Mr Mrs Ms Dr Eng Sir Madam
Beirut Lebanon Riyadh Jeddah Dubai Saudi Arabia UAE KSA Qatar Kuwait Bahrain Oman Jordan Egypt
""".split())

_LATIN = sorted(set(LATIN_FIRST_NAMES), key=len, reverse=True)
_ARABIC = sorted(set(ARABIC_FIRST_NAMES), key=len, reverse=True)

_SURNAME_WORD = r"[A-Z][A-Za-z'’-]+"
_LATIN_RE = re.compile(
    r"(?<![\w@.])"
    r"(?P<first>(?:" + "|".join(_LATIN) + r")|(?:" + "|".join(n.upper() for n in _LATIN) + r"))"
    r"(?P<rest>(?:[ \t]+(?:(?:Abou|Abu|Bou|Abi|Abdel|Abdul|Abd|Al|El|Bin|Ben|Ibn|Bint|ABOU|ABU|BOU|ABI|ABDEL|AL|EL)[ \t]+)?" + _SURNAME_WORD + r"){0,2})"
    r"(?![\w@])"
)
_ARABIC_LETTER = "؀-ۿ"
_ARABIC_RE = re.compile(
    rf"(?<![{_ARABIC_LETTER}])(?:[وبل](?=(?:" + "|".join(_ARABIC) + rf")(?![{_ARABIC_LETTER}])))?"
    r"(?P<first>(?:" + "|".join(_ARABIC) + rf"))(?![{_ARABIC_LETTER}])"
)


def _trim_rest(rest):
    """Keep surname words until one is a stop word."""
    kept = []
    for word in rest.split():
        if word in NOT_SURNAMES or word.capitalize() in NOT_SURNAMES:
            break
        kept.append(word)
    # Don't end on a dangling particle ("Rami Abou").
    while kept and kept[-1].lower() in PARTICLES:
        kept.pop()
    return kept


from spacy.lang.en.stop_words import STOP_WORDS  # noqa: E402

_LATIN_LOWER = {n.lower() for n in _LATIN}
_LOWER_RE = re.compile(
    r"(?<![\w@.])(?P<first>" + "|".join(sorted(_LATIN_LOWER, key=len, reverse=True)) + r")"
    r"(?P<rest>(?:[ \t]+[a-z][a-z'-]+){1,3})(?![\w@])"
)


def _lower_name_span(first, rest):
    """For an all-lowercase name like 'naya alain kanaan'.

    Lowercase text is risky ("dana said"), so this only fires for the common
    Lebanese pattern of first name + father's name (both on the list), plus an
    optional family name that is not an everyday English word.
    """
    words = rest.split()
    if not words or (words[0] not in _LATIN_LOWER and words[0] not in PARTICLES):
        return None
    kept = [words[0]]
    for w in words[1:]:
        if w in STOP_WORDS or len(w) < 3:
            break
        kept.append(w)
        if w not in PARTICLES:
            break
    while kept and kept[-1] in PARTICLES:
        kept.pop()
    return kept or None


class NameListRecognizer(EntityRecognizer):
    """Finds common Lebanese/Arab first names and the surname after them."""

    def __init__(self):
        super().__init__(supported_entities=["PERSON"], name="ArabNameListRecognizer", supported_language="en")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if "PERSON" not in entities:
            return []
        results = []
        for m in _LATIN_RE.finditer(text):
            start = m.start("first")
            end = m.end("first")
            kept = _trim_rest(m.group("rest"))
            if kept:
                # extend end over the kept words exactly as they appear
                pos = end
                for word in kept:
                    pos = text.index(word, pos) + len(word)
                end = pos
            results.append(RecognizerResult("PERSON", start, end, 0.75))
        for m in _LOWER_RE.finditer(text):
            kept = _lower_name_span(m.group("first"), m.group("rest"))
            if not kept:
                continue
            pos = m.end("first")
            for word in kept:
                pos = text.index(word, pos) + len(word)
            results.append(RecognizerResult("PERSON", m.start("first"), pos, 0.7))
        for m in _ARABIC_RE.finditer(text):
            results.append(RecognizerResult("PERSON", m.start("first"), m.end("first"), 0.75))
        return results


def _name_parts(full):
    """Single words of a name worth hiding on their own ("Rami", "Haddad")."""
    parts = []
    for w in re.split(r"\s+", full.strip()):
        bare = w.strip(".,;:()[]\"'")
        if len(bare) < 3 or bare.lower() in PARTICLES or bare in NOT_SURNAMES or not _NAME_WORD.fullmatch(bare):
            continue
        parts.append(bare)
    return parts if len(parts) > 1 else []


def propagate_names(text, results, known_names):
    """Add every other mention of a detected or previously seen name."""
    from presidio_analyzer import RecognizerResult as RR

    names = {text[r.start:r.end] for r in results if r.entity_type == "PERSON"} | set(known_names)
    variants = set()
    for n in names:
        n = n.strip()
        if len(n) < 2:
            continue
        variants.add(n)
        variants.update(_name_parts(n))
    if not variants:
        return results

    taken = [(r.start, r.end) for r in results]

    def free(s, e):
        return all(e <= a or s >= b for a, b in taken)

    added = []
    for v in sorted(variants, key=len, reverse=True):
        for m in re.finditer(r"(?<![\w@.])" + re.escape(v) + r"(?![\w@])", text, flags=re.IGNORECASE):
            found = m.group(0)
            # Lowercase single words only when they are not everyday English
            # ("kanaan" yes, "will" no).
            if found[0].islower() and " " not in v and (v.lower() in STOP_WORDS or len(v) < 3):
                continue
            if free(m.start(), m.end()):
                taken.append((m.start(), m.end()))
                added.append(RR("PERSON", m.start(), m.end(), 0.8))
    return results + added


def person_tag_for(value, person_bucket):
    """Reuse a person's placeholder when one name is clearly part of another.

    "Rami" after "Rami Haddad" (or the reverse) gets the same tag, but only when
    exactly one known person matches, so two different Ramis stay apart.
    """
    low = value.lower()
    words = [w.lower() for w in value.split()]
    matches = set()
    for known, tag in person_bucket.items():
        k_words = [w.lower() for w in known.split()]
        if len(words) == 1 and low in k_words and len(k_words) > 1:
            matches.add(tag)
        elif len(k_words) == 1 and len(words) > 1 and k_words[0] in words:
            matches.add(tag)
        elif known.lower() == low:
            matches.add(tag)
    return matches.pop() if len(matches) == 1 else None


_NAME_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|['\u2019.-])*")


def clean_person_spans(text, results):
    """Trim stop words off PERSON spans and drop spans that are only stop words.

    The language model sometimes tags a heading word ("Sami Project") or a
    label ("Email") as part of a name.
    """
    from presidio_analyzer import RecognizerResult as RR

    out = []
    for r in results:
        if r.entity_type != "PERSON":
            out.append(r)
            continue
        words = list(re.finditer(r"\S+", text[r.start:r.end]))
        # A name never contains a number or a bare symbol: cut the span at the
        # first such word ("thomma" + next table cell "25.00").
        for k, m in enumerate(words):
            if not _NAME_WORD.fullmatch(m.group(0).strip(".,;:()[]\"'")):
                words = words[:k]
                break
        def stop(m):
            w = m.group(0).strip(".,;:()[]\"'")
            return w in NOT_SURNAMES or w.capitalize() in NOT_SURNAMES
        while words and stop(words[0]):
            words.pop(0)
        while words and (stop(words[-1]) or words[-1].group(0).lower() in PARTICLES):
            words.pop()
        if not words:
            continue
        s, e = r.start + words[0].start(), r.start + words[-1].end()
        out.append(RR("PERSON", s, e, r.score) if (s, e) != (r.start, r.end) else r)
    return out
