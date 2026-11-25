"""Unit tests for domain normalization and filtering (`src.models` & `src.parser`)."""

import unittest

from pydantic import ValidationError

from src.models import (
    SLOVAK_DISTRICTS,
    ApartmentModel,
    extract_floor_and_total_from_text,
    has_balcony_from_text,
    has_negated_balcony,
)
from src.parser import resolve_balcony_from_attrs


class TestBalconyThreeValuedAndStemming(unittest.TestCase):
    """Verify three-valued logic (`True` / `False` / `None`) and morphological stem matching."""

    def test_morphological_stems_positive(self) -> None:
        positive_samples = [
            "Slnečný 2-izbový byt s balkónom",
            "Byt disponuje priestrannou loggiou",
            "3-izbový byt s lodžiou 69 m2",
            "DISPOZÍCIA:\n- obývacia izba\n- terasa\n- predzáhradka",
            "Svet vonku spomaľuje z pohodlia vašej terasy",
            "Unikátna je tiež terasa s presklenými zábradliami",
            "Predaj 3 izbový byt, teraska, Jelenecká ul.",
            "Byt s vlastným vstupom, vlastnou terasou na prízemí",
        ]
        for text in positive_samples:
            with self.subTest(text=text):
                self.assertTrue(has_balcony_from_text(text))

        # Winter garden ("zimná záhrada") alone is not a balcony/loggia/terrace
        self.assertFalse(has_balcony_from_text("Priestranný 2-izbový byt so zimnou záhradou"))

    def test_explicit_negations(self) -> None:
        negated_samples = [
            "Ponúkam na predaj 1-izbový byt bez balkóna",
            "• Výťah: nie • Balkón: nie",
            "Poznámka: Byt nemá balkón",
            "Byt je orientovaný na západ. Bez lodžie.",
            "Terasa : nie, Poschodie : 6/7",
            "Byty nemajú balkóny.",
        ]
        for text in negated_samples:
            with self.subTest(text=text):
                self.assertTrue(has_negated_balcony(text))
                self.assertFalse(has_balcony_from_text(text))

    def test_kosice_terasa_homonym_masked(self) -> None:
        kosice_samples = [
            "1 izbový byt Predaj Košice II Terasa Muškátová ul.",
            "Moderný 1,5-izbový byt na Terase, Popradská ulica",
            "predam 1 izbový byt na terase",
            "KOMPLETNE ZREKONŠTRUOVANÝ 3-IZBOVÝ BYT NA TERASE – KOŠICE",
            "V ponuke na predaj 3 izbový byt na ulici Trieda SNP – Terasa v Košiciach",
            "Bývanie na Terase s mestom na dosah, 2 izbový byt, Košice – Západ",
            "https://www.nehnutelnosti.sk/detail/JuinPF3l-x5/na-predaj-moderny-15-izbovy-byt-na-terase",
            "https://www.nehnutelnosti.sk/detail/Ju7mrwU9Tsr/1-izbovy-byt-predaj-kosice-ii-terasa-muskatova-ul",
        ]
        for text in kosice_samples:
            with self.subTest(text=text):
                self.assertFalse(has_balcony_from_text(text))

    def test_three_valued_attrs_resolution(self) -> None:
        self.assertIsNone(resolve_balcony_from_attrs({}, ""))
        self.assertTrue(resolve_balcony_from_attrs({"Plocha balkóna": "4,5 m²"}, ""))
        self.assertTrue(resolve_balcony_from_attrs({"Terás": "1"}, ""))
        self.assertFalse(resolve_balcony_from_attrs({"Počet balkónov": "0"}, ""))
        self.assertFalse(resolve_balcony_from_attrs({"Loggia": "Nie"}, ""))

    def test_model_three_valued_balcony(self) -> None:
        # 1. Unspecified -> None (NULL)
        apt_unknown = ApartmentModel(
            id="JuUnknown",
            url="https://www.nehnutelnosti.sk/detail/JuUnknown/2-izbovy-byt",
            title="Predaj 2-izbový byt v centre mesta",
            category_sub="TWO_ROOM_APARTMENT",
            city="Nitra",
            district="Nitra",
        )
        self.assertIsNone(apt_unknown.has_balcony)

        # 2. Negated in title -> False
        apt_false = ApartmentModel(
            id="JuFalse",
            url="https://www.nehnutelnosti.sk/detail/JuFalse/1-izbovy-byt",
            title="Predaj 1-izbový byt bez balkóna",
            category_sub="ONE_ROOM_APARTMENT",
            city="Nitra",
            district="Nitra",
        )
        self.assertFalse(apt_false.has_balcony)

        # 3. Confirmed in title -> True
        apt_true = ApartmentModel(
            id="JuTrue",
            url="https://www.nehnutelnosti.sk/detail/JuTrue/3-izbovy-byt",
            title="Predaj 3 izbový byt, teraska, Jelenecká ul.",
            category_sub="THREE_ROOM_APARTMENT",
            city="Nitra",
            district="Nitra",
        )
        self.assertTrue(apt_true.has_balcony)

        # 4. Shared staircase/landing balcony ("spoločný balkón na medziposchodí") -> False
        apt_shared = ApartmentModel(
            id="JuShared",
            url="https://www.nehnutelnosti.sk/detail/JuShared/garsonka-vrakuna",
            title="Garsónka, 35 m2, Bratislava - Vrakuňa",
            description="K bytu prislúcha pivnica a spoločný balkón na medziposchodí.",
            category_sub="STUDIO_APARTMENT",
            has_balcony=True,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertFalse(apt_shared.has_balcony)


class TestRoomCountResolution(unittest.TestCase):
    """Verify room count priority between structured `category_sub` and `title`."""

    def test_developer_multi_room_ranges_defer_to_category_sub(self) -> None:
        cases = [
            ("ISTRICA - NOVÉ 1- AŽ 4-IZBOVÉ BYTY V PRÍRODE", "TWO_ROOM_APARTMENT", 2),
            ("AVILA-prvé skolaudované 1, 2 a 3 izbové byty odovzdané", "TWO_ROOM_APARTMENT", 2),
            ("Na predaj 2-3 izbový byt v centre mesta Nové Zámky", "TWO_ROOM_APARTMENT", 2),
            ("V predpredaji byty 1 a 2 izb. projekt DB-Nitra", "ONE_ROOM_APARTMENT", 1),
            ("Nitra Centrum ul. Janka Kráľa novostavba 2,3 izbové byty", "THREE_ROOM_APARTMENT", 3),
        ]
        for title, subcat, expected in cases:
            with self.subTest(title=title):
                apt = ApartmentModel(
                    id="JuTest",
                    url="https://www.nehnutelnosti.sk/detail/JuTest/x",
                    title=title,
                    category_sub=subcat,
                    city="Nitra",
                    district="Nitra",
                )
                self.assertEqual(apt.rooms_count, expected)

    def test_hypothetical_vs_completed_conversions(self) -> None:
        # Hypothetical conversions must keep current category_sub room count
        apt_hyp1 = ApartmentModel(
            id="JuHyp1",
            url="https://www.nehnutelnosti.sk/detail/JuHyp1/x",
            title="**Možnosť prerobenia na 4-izbový byt, TOP LOKALITA**",
            category_sub="THREE_ROOM_APARTMENT",
            city="Bratislava",
            district="Bratislava III",
        )
        self.assertEqual(apt_hyp1.rooms_count, 3)

        apt_hyp2 = ApartmentModel(
            id="JuHyp2",
            url="https://www.nehnutelnosti.sk/detail/JuHyp2/x",
            title="Vzdušný 100 m² mezonet | Flexibilná dispozícia až pre 5 izieb",
            category_sub="FOUR_ROOM_APARTMENT",
            city="Bratislava",
            district="Bratislava III",
        )
        self.assertEqual(apt_hyp2.rooms_count, 4)

        # Completed conversions (even with "investičný potenciál") must resolve to converted count
        apt_conv1 = ApartmentModel(
            id="JuConv1",
            url="https://www.nehnutelnosti.sk/detail/JuConv1/x",
            title="1-izbový byt prerobený na 2-izbový | 47 m² |investičný potenciál",
            category_sub="ONE_ROOM_APARTMENT",
            city="Košice",
            district="Košice I",
        )
        self.assertEqual(apt_conv1.rooms_count, 2)

        apt_conv2 = ApartmentModel(
            id="JuConv2",
            url="https://www.nehnutelnosti.sk/detail/JuConv2/x",
            title="Dispozične upravený 2-izbový byt na 3-izbový v srdci Palisád – s investičným potenciálom",
            category_sub="TWO_ROOM_APARTMENT",
            city="Bratislava",
            district="Bratislava I",
        )
        self.assertEqual(apt_conv2.rooms_count, 3)

    def test_edge_case_tokens(self) -> None:
        # "28 m² garsónka" must not match "2-garsónka" after NFKD "²" -> "2"
        apt_gar = ApartmentModel(
            id="JuGar",
            url="https://www.nehnutelnosti.sk/detail/JuGar/x",
            title="PREDAJ | Slnečná 28 m² garsónka s novou kúpeľňou",
            category_sub="STUDIO_APARTMENT",
            city="Bratislava",
            district="Bratislava V",
        )
        self.assertEqual(apt_gar.rooms_count, 1)

        # ",2-garsónku" must match 2
        apt_2gar = ApartmentModel(
            id="Ju2Gar",
            url="https://www.nehnutelnosti.sk/detail/Ju2Gar/x",
            title="Ponúkame na predaj,2-garsónku na Stavbárskej ulici",
            category_sub="DOUBLE_STUDIO_APARTMENT",
            city="Bratislava",
            district="Bratislava II",
        )
        self.assertEqual(apt_2gar.rooms_count, 2)

        # "PROKEKT-ZWIRN 3,2-IZBOVÝ BYT" must match 2, not 3
        apt_zwirn = ApartmentModel(
            id="JuZwirn",
            url="https://www.nehnutelnosti.sk/detail/JuZwirn/x",
            title="PROKEKT-ZWIRN 3,2-IZBOVÝ BYT,NOVOSTAVBA,RUŽINOV-NIVY",
            category_sub="TWO_ROOM_APARTMENT",
            city="Bratislava",
            district="Bratislava II",
        )
        self.assertEqual(apt_zwirn.rooms_count, 2)


class TestDistrictAndEntityFiltering(unittest.TestCase):
    """Verify Slovak district whitelist and non-apartment rejection rules."""

    def test_slovak_districts_count(self) -> None:
        # 79 official districts + 2 city-level fallbacks ("Bratislava", "Košice") = 81
        self.assertEqual(len(SLOVAK_DISTRICTS), 81)

    def test_foreign_district_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuForeignDist",
                url="https://www.nehnutelnosti.sk/detail/JuForeignDist/x",
                title="2-izbový byt",
                city="Wien",
                district="Wien",
            )

    def test_non_apartment_entities_rejected(self) -> None:
        rejected_titles = [
            "Predaj apartmánu (1 izbový) 74.07 m2, Tbilisi Gruzínsko",
            "R E N A T A S - Investičná príležitosť v Dominikánskej republike",
            "ZA CENU mini GARSONKY V BRATISLAVE VLASTNÝ RODINNÝ DOMČEK V RAKÚSKU - WOLFSTHAL",
            "PREDAJ GARÁŽE, samostatná garáž v garážovom dome",
            "Pivničná kobka 3,5 m2 na predaj, Bratislava - Ružinov",
            "63m2 - INDUSTRIÁLNA VODÁRENSKÁ STUDŇA - LESOPARK VRAKUŇA",
            "Nebytový priestor vhodný na bývanie / podnikanie Kramare BA",
            "Predaj moderného 5-izbového rodinného domu v radovej zástavbe v Bernolákove.",
            "Dva byty v rodinnom dome, Žilina - Rosina",
            "NA PREDAJ ZAUJÍMAVÝ PRIESTOR S POZEMKOM A PARKOVANÍM BRATISLAVA VRAKUŇA",
            "5i byt B so záhradou v trojdome",
            "Vrakuňa 5i byt v RD so záhradou + parking",
            "RADO | 2 byty v 1 už s nájomníkmi | okamžitý príjem 1600 €/mes",
            "2 bytové jednotky za cenu bytu na ulici Wolkrova",
            "Z Petržalky za záhradou: Kompletne zariadený 2-izbový byt v Hamuliakove",
            "TREEF | INVESTIČNÉ BYTY v TT kraji ? OBJAVTE NOVOSTAVBU V DVORNÍKOCH",
        ]
        for title in rejected_titles:
            with self.subTest(title=title), self.assertRaises(ValidationError):
                ApartmentModel(
                    id="JuReject",
                    url="https://www.nehnutelnosti.sk/detail/JuReject/x",
                    title=title,
                    category_sub="TWO_ROOM_APARTMENT",
                    city="Bratislava",
                    district="Bratislava V",
                )

    def test_description_and_structural_townhouse_rejection(self) -> None:
        # Townhouse with private land plot and low-rise footprint
        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuLavender",
                url="https://www.nehnutelnosti.sk/detail/JuLavender/x",
                title="4-izbový byt s vlastnou záhradou",
                description="Zabudnite na predstavu klasického bytu v bytovom dome.",
                category_sub="FOUR_ROOM_APARTMENT",
                area_sqm=97.0,
                floor=1,
                total_floors=2,
                plocha_pozemku=157.0,
                zastavana_plocha=92.0,
                city="Bratislava-Vrakuňa",
                district="Bratislava II",
            )

        # Non-residential space on title deed ("na LV vedený ako priestor")
        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuLvPriestor",
                url="https://www.nehnutelnosti.sk/detail/JuLvPriestor/x",
                title="2-izbový byt s veľkou terasou",
                description="Nehnuteľnosť je na liste vlastníctva vedená ako nebytový priestor.",
                category_sub="TWO_ROOM_APARTMENT",
                area_sqm=53.0,
                city="Bratislava-Vrakuňa",
                district="Bratislava II",
            )

    def test_legitimate_apartments_with_house_metaphors_pass(self) -> None:
        valid_titles = [
            "EXKLUZÍVNE - Veľký 3i byt s benefitmi rodinného domu, Boleráz",
            "Na predaj investičný 3-izbový apartmán (nebytový priestor) v Bardejove",
            "Váš nový rodinný domov v Stupave? 85 m² priestoru a pokoja",
            "SKOLAUDOVANÉ, 2 IZB. NOVOSTAVBA S TERASOU, ZOCHOVÁ CHATA, MODRA",
            "SISTERS | BÝVAJTE AKO V DOME! 3-IZBOVÁ NOVOSTAVBA S VLASTNOU ZÁHRADOU",
            "DO DOMČEKA | Výnimočné bývanie v historickom dome v centre mesta",
            "AFYREAL predaj 4izb, top lokalita, loggia, parkovisko a MHD pod domom",
            "Rezidencia Karpatská - DOM D - 3 izbový by D.1A - 1.NP + predzáhradka",
            "4-IZBOVÝ BYT S VEĽKORYSOU LOGGIOU | MOŽNOSŤ 2 BYTOVÝCH JEDNOTIEK",
            "Štýlový 2-izbový byt 75 m² s balkónom v obľúbenom projekte 500 bytov",
            "SKOLAUDOVANÉ! - 2-izbový byt C.III.3 s lodžiou, v cene bytu pivničná kobka",
            "Oáza mestskej elegancie a pokoja v lukratívnom srdci Ružinova",
            "Bývanie, kde má priestor všetko dôležité | 4-izbový | 81 m² | Piešťany",
            "3i chyžka s loggiou na Zvončekovej",
        ]
        for title in valid_titles:
            with self.subTest(title=title):
                apt = ApartmentModel(
                    id="JuValid",
                    url="https://www.nehnutelnosti.sk/detail/JuValid/x",
                    title=title,
                    category_sub="THREE_ROOM_APARTMENT",
                    area_sqm=68.0,
                    city="Trnava",
                    district="Trnava",
                )
                self.assertEqual(apt.id, "JuValid")

    def test_configurable_reserved_filter(self) -> None:
        # By default (exclude_reserved=False), reserved listings are kept for analytics
        apt_kept = ApartmentModel(
            id="JuRes1",
            url="https://www.nehnutelnosti.sk/detail/JuRes1/x",
            title="REZERVOVANÉ | Slnečný 2-izbový byt s balkónom",
            category_sub="TWO_ROOM_APARTMENT",
            price_eur=185000,
            area_sqm=54.0,
            city="Nitra",
            district="Nitra",
        )
        self.assertEqual(apt_kept.rooms_count, 2)
        self.assertTrue(apt_kept.has_balcony)

        # When exclude_reserved=True is passed, reserved listings are rejected
        with self.assertRaises(ValidationError):
            ApartmentModel(
                id="JuRes1",
                url="https://www.nehnutelnosti.sk/detail/JuRes1/x",
                title="REZERVOVANÉ | Slnečný 2-izbový byt s balkónom",
                category_sub="TWO_ROOM_APARTMENT",
                price_eur=185000,
                area_sqm=54.0,
                city="Nitra",
                district="Nitra",
                exclude_reserved=True,
            )


class TestCrossFieldPhysicalPlausibility(unittest.TestCase):
    """Verify area_sqm cross-field anomaly correction, floor abbreviations, and condition resolution."""

    def test_missing_decimal_point_area_correction(self) -> None:
        apt = ApartmentModel(
            id="JuTypo",
            url="https://www.nehnutelnosti.sk/detail/JuTypo/x",
            title="Apartmán na predaj, 2. izbový, Podskalné, Bojnice",
            category_sub="TWO_ROOM_APARTMENT",
            area_sqm=392.0,
            price_eur=258300,
            city="Bojnice",
            district="Prievidza",
        )
        self.assertEqual(apt.area_sqm, 39.2)
        self.assertEqual(apt.price_per_sqm_eur, round(258300 / 39.2, 2))

    def test_garden_terrace_sum_corrected_from_title(self) -> None:
        apt = ApartmentModel(
            id="JuSum",
            url="https://www.nehnutelnosti.sk/detail/JuSum/x",
            title="Na PREDAJ novostavba 3i bytu 87m2 s dvomi terasami 73m2 v Centre mesta",
            category_sub="THREE_ROOM_APARTMENT",
            area_sqm=160.0,
            price_eur=230000,
            city="Nitra",
            district="Nitra",
        )
        self.assertEqual(apt.area_sqm, 87.0)

    def test_floor_abbreviations_and_ground_floor_normalization(self) -> None:
        self.assertEqual(extract_floor_and_total_from_text("Byt 4.p/5, Košice"), (4, 5))
        self.assertEqual(extract_floor_and_total_from_text("2-izbový byt, 54 m2, 2.posch."), (2, None))
        self.assertEqual(extract_floor_and_total_from_text("Prízemný 1,5 izbový tehlový byt"), (0, None))

        # 1. NP / Zvýšené prízemie / Umiestnenie=Prízemie must normalize floor 1 -> 0
        apt_ground = ApartmentModel(
            id="JuGround",
            url="https://www.nehnutelnosti.sk/detail/JuGround/x",
            title="2-izbový byt v novostavbe",
            description="Byt sa nachádza na 1. nadzemnom podlaží v 5-poschodovom bytovom dome.",
            umiestnenie="Prízemie",
            category_sub="TWO_ROOM_APARTMENT",
            floor=1,
            total_floors=5,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertEqual(apt_ground.floor, 0)
        self.assertEqual(apt_ground.total_floors, 5)

        # 5/5 NP where description states "na 4. poschodí" must normalize floor 5 -> 4
        apt_np = ApartmentModel(
            id="JuNpTop",
            url="https://www.nehnutelnosti.sk/detail/JuNpTop/x",
            title="3-izbový byt",
            description="Byt sa nachádza na 4. poschodí zatepleného bytového domu.",
            category_sub="THREE_ROOM_APARTMENT",
            floor=5,
            total_floors=5,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertEqual(apt_np.floor, 4)

    def test_interior_floor_area_decomposition(self) -> None:
        # 1. Explicit Podlahová plocha + loggia breakdown
        apt1 = ApartmentModel(
            id="JuArea1",
            url="https://www.nehnutelnosti.sk/detail/JuArea1/x",
            title="1-izbový byt v novostavbe",
            description="Podlahová plocha bytu je 36,10 m² + 3,45 m² loggia.",
            category_sub="ONE_ROOM_APARTMENT",
            area_sqm=40.0,
            price_eur=139900,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertEqual(apt1.area_sqm, 36.1)

        # 2. Additive breakdown with balcony and cellar
        apt2 = ApartmentModel(
            id="JuArea2",
            url="https://www.nehnutelnosti.sk/detail/JuArea2/x",
            title="3-izbový byt",
            description="Rozloha bytu je 64,76 m2 + 3,97 m2 loggia + 0,95 m2 pivnica.",
            category_sub="THREE_ROOM_APARTMENT",
            area_sqm=70.0,
            price_eur=209000,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertEqual(apt2.area_sqm, 64.76)

        # 3. Subtractive breakdown ("celková výmera 70,00 m² z toho loggia 3,78 m²")
        apt3 = ApartmentModel(
            id="JuArea3",
            url="https://www.nehnutelnosti.sk/detail/JuArea3/x",
            title="3-izbový byt",
            description="Celková výmera bytu je 70,00 m2, z toho loggia 3,78 m2.",
            category_sub="THREE_ROOM_APARTMENT",
            area_sqm=70.0,
            price_eur=199900,
            city="Bratislava-Vrakuňa",
            district="Bratislava II",
        )
        self.assertEqual(apt3.area_sqm, 66.22)

    def test_modern_complex_and_condition_upgrade(self) -> None:
        apt = ApartmentModel(
            id="JuSlnecnice",
            url="https://www.nehnutelnosti.sk/detail/JuSlnecnice/x",
            title="2-izbový byt, Slnečnice, Bratislava - Petržalka",
            category_sub="TWO_ROOM_APARTMENT",
            property_condition="Pôvodný stav",
            has_elevator=False,
            floor=3,
            total_floors=6,
            city="Bratislava-Petržalka",
            district="Bratislava V",
        )
        self.assertEqual(apt.property_condition, "Novostavba")
        self.assertTrue(apt.has_elevator)

    def test_area_balcony_floor_edge_cases(self) -> None:
        # Adjacent buyable apartment in description must not overwrite main apartment interior area
        apt_adj = ApartmentModel(
            id="JuAdj1",
            url="https://www.nehnutelnosti.sk/detail/JuAdj1/x",
            title="SVOBODA & WILLIAMS | 4-izbový byt na Slavíne so záhradou",
            category_sub="FOUR_ROOM_APARTMENT",
            price_eur=1430906,
            area_sqm=330.0,
            city="Bratislava-Staré Mesto",
            district="Bratislava I",
            description="Interiér 174 m2, terasa 152 m2, pivnica 4 m2. Možnosť dokúpiť aj vedľajší byt 164 m² (90 m² interiér).",
        )
        self.assertEqual(apt_adj.area_sqm, 174.0)

        # Comparative "O 12 m² viac" in title must not replace 62 m² area
        apt_comp = ApartmentModel(
            id="JuComp1",
            url="https://www.nehnutelnosti.sk/detail/JuComp1/x",
            title="O 12 m² VIAC AKO BEŽNÝ 2-IZBÁK – 62 m² TEHLA, ŽILINA CENTRUM",
            category_sub="TWO_ROOM_APARTMENT",
            price_eur=157490,
            area_sqm=62.0,
            city="Žilina",
            district="Žilina",
        )
        self.assertEqual(apt_comp.area_sqm, 62.0)

        # Area exclusion "(bez balkóna a pivnice)" must not negate balcony
        apt_balc = ApartmentModel(
            id="JuBalcExcl1",
            url="https://www.nehnutelnosti.sk/detail/JuBalcExcl1/x",
            title="Predaj 2-i. bytu, 60 m2, Na Zongorke, Trenčín",
            category_sub="TWO_ROOM_APARTMENT",
            price_eur=174900,
            area_sqm=60.0,
            city="Trenčín",
            district="Trenčín",
            description="Byt s výmerou 60,30 m² (bez balkóna a pivnice) a výstupom na balkón (2,70 m²).",
        )
        self.assertTrue(apt_balc.has_balcony)

        # Elevator stop "výťah po 4. poschodie" must not shift 5/5 floor to 4
        apt_fl = ApartmentModel(
            id="JuElevStop1",
            url="https://www.nehnutelnosti.sk/detail/JuElevStop1/x",
            title="Klimatizovaný, slnečný 3i byt v srdci Rače",
            category_sub="THREE_ROOM_APARTMENT",
            price_eur=241900,
            area_sqm=60.0,
            floor=5,
            total_floors=5,
            city="Bratislava-Rača",
            district="Bratislava III",
            description="- podlahová plocha bytu: 48,34 m² + zasklená loggia: 11,73 m²\n- 5. poschodie z 5\n- 1x výťah - výťah po 4. poschodie",
        )
        self.assertEqual(apt_fl.floor, 5)
        self.assertEqual(apt_fl.area_sqm, 48.34)


if __name__ == "__main__":
    unittest.main()
