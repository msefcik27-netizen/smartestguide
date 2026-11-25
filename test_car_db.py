# Testy databáze rozměrů aut (car_db.py) a jejího napojení v parking.py. Spuštění: python -m pytest test_car_db.py -q
import car_db
import parking as pk

ROWS = car_db.prepare([
    {"make": "Škoda", "model": "Octavia", "variant": "Liftback", "generation": "IV", "year_from": 2020, "year_to": None,
     "length_mm": 4698, "width_mm": 1829, "height_mm": 1470, "source": "https://example.com/octavia"},
    {"make": "Škoda", "model": "Octavia", "variant": "Combi", "generation": "IV", "year_from": 2020, "year_to": None,
     "length_mm": 4698, "width_mm": 1829, "height_mm": 1468, "source": "https://example.com/octavia-combi"},
    {"make": "Škoda", "model": "Octavia", "variant": "Combi", "generation": "III", "year_from": 2013, "year_to": 2019,
     "length_mm": 4667, "width_mm": 1814, "height_mm": 1465, "source": "https://example.com/octavia3"},
    {"make": "Toyota", "model": "Yaris", "variant": "", "generation": "", "year_from": 2020, "year_to": None,
     "length_mm": 3940, "width_mm": 1745, "height_mm": 1500, "source": "https://example.com/yaris"},
    {"make": "Toyota", "model": "Yaris Cross", "variant": "", "generation": "", "year_from": 2021, "year_to": None,
     "length_mm": 4180, "width_mm": 1765, "height_mm": 1595, "source": "https://example.com/yaris-cross"},
    {"make": "BMW", "model": "3 Series", "variant": "Touring", "generation": "G21", "year_from": 2019, "year_to": None,
     "length_mm": 4713, "width_mm": 1827, "height_mm": 1440, "source": "https://example.com/bmw3"},
    {"make": "Mercedes-Benz", "model": "C-Class", "variant": "sedan", "generation": "W206", "year_from": 2021, "year_to": None,
     "length_mm": 4751, "width_mm": 1820, "height_mm": 1438, "source": "https://example.com/c"},
    {"make": "Land Rover", "model": "Range Rover Sport", "variant": "", "generation": "", "year_from": 2022, "year_to": None,
     "length_mm": 4946, "width_mm": 2047, "height_mm": 1820, "source": "https://example.com/rrs"},
    {"make": "Volkswagen", "model": "ID.3", "variant": "", "generation": "", "year_from": 2020, "year_to": None,
     "length_mm": 4264, "width_mm": 1809, "height_mm": 1564, "source": "https://example.com/id3"},
    {"make": "Nesmysl", "model": "X", "length_mm": 99999, "width_mm": 1800, "height_mm": 1500, "source": "x"},   # mimo rozsah
    {"make": "", "model": "Bez značky", "length_mm": 4000, "width_mm": 1800, "height_mm": 1500},
])


def test_prepare_drops_invalid_and_rounds_up_to_cm():
    assert len(ROWS) == 9
    e = [r for r in ROWS if r["model"] == "octavia" and r["variant"] == "liftback"][0]
    assert e["cm"] == (147, 183, 470)          # 1470 → 147, 1829 → 183 (nahoru), 4698 → 470 (nahoru)
    assert e["make"] == "skoda" and e["label"] == "Škoda Octavia Liftback"
    assert car_db.prepare(None) == [] and car_db.prepare([1, "x", {}]) == []


def test_lookup_corrects_model_dimensions():
    hit = car_db.lookup("Škoda Octavia Combi 2021", (147, 183, 469), ROWS)
    assert hit["car"] == (147, 183, 470) and hit["label"] == "Škoda Octavia Combi"
    assert hit["source"] == "https://example.com/octavia-combi"


def test_year_picks_generation_and_variant_synonyms():
    hit = car_db.lookup("Skoda Octavia kombi 2016", (147, 182, 466), ROWS)
    assert hit["car"] == (147, 182, 467) and hit["source"].endswith("octavia3")
    assert car_db.lookup("Škoda Octavia Estate 2021", (146, 183, 469), ROWS)["label"] == "Škoda Octavia Combi"


def test_longest_model_wins():
    assert car_db.lookup("Toyota Yaris Cross 2022", (160, 177, 418), ROWS)["label"] == "Toyota Yaris Cross"
    assert car_db.lookup("Toyota Yaris 2021", (150, 175, 394), ROWS)["label"] == "Toyota Yaris"


def test_too_different_dimensions_mean_no_match():
    # Model myslí jiné auto / jinou generaci (rozdíl přes TOL_CM) → databáze se nepoužije.
    assert car_db.lookup("Škoda Octavia Combi", (147, 183, 440), ROWS) is None
    assert car_db.lookup("Škoda Octavia Combi", (175, 183, 469), ROWS) is None       # se střešním boxem


def test_unknown_dimension_stays_unknown():
    hit = car_db.lookup("Škoda Octavia Combi 2021", (None, 183, 469), ROWS)
    assert hit["car"] == (None, 183, 470)


def test_no_match_cases():
    assert car_db.lookup("Škoda Superb 2021", (148, 186, 487), ROWS) is None           # model v tabulce není
    assert car_db.lookup("Octavia", (147, 183, 469), ROWS) is None                     # bez značky
    assert car_db.lookup("", (147, 183, 469), ROWS) is None
    assert car_db.lookup("Škoda Octavia", None, ROWS) is None
    assert car_db.lookup("Škoda Octavia", (None, None, None), ROWS) is None
    assert car_db.lookup("Toyota Yarisa", (150, 175, 394), ROWS) is None               # jen celé slovo


def test_tie_between_different_dimensions_is_no_match():
    rows = car_db.prepare([
        {"make": "Test", "model": "Car", "variant": "A", "length_mm": 4500, "width_mm": 1800, "height_mm": 1500, "source": "s"},
        {"make": "Test", "model": "Car", "variant": "B", "length_mm": 4520, "width_mm": 1800, "height_mm": 1500, "source": "s"},
    ])
    assert car_db.lookup("Test Car", (150, 180, 451), rows) is None
    assert car_db.lookup("Test Car B", (150, 180, 451), rows)["car"] == (150, 180, 452)


def test_names_written_differently():
    assert car_db.canon("VW ID3") == "volkswagen id 3"
    assert car_db.lookup("VW ID.3 2022", (157, 181, 426), ROWS)["label"] == "Volkswagen ID.3"
    for name in ("BMW 320d Touring", "BMW řady 3 Touring", "BMW 3er Touring", "BMW 3 Touring 2020", "BMW 3 Series Touring"):
        assert car_db.lookup(name, (144, 183, 471), ROWS)["label"] == "BMW 3 Series Touring", name
    for name in ("Mercedes C 220 d sedan", "Mercedes-Benz třída C", "Mercedes C-Klasse 2022", "Mercedes-Benz C-Class"):
        assert car_db.lookup(name, (144, 182, 475), ROWS)["label"] == "Mercedes-Benz C-Class sedan", name
    assert car_db.lookup("Range Rover Sport 2023", (182, 205, 495), ROWS)["label"] == "Land Rover Range Rover Sport"


def test_real_table_loads_and_is_sane():
    rows = car_db.load()
    assert len(rows) > 300
    assert all(r["source"].startswith("https://") for r in rows)
    assert all(110 <= r["cm"][0] <= 310 and 140 <= r["cm"][1] <= 230 and 240 <= r["cm"][2] <= 750 for r in rows)
    hit = car_db.lookup("Škoda Octavia Combi 2021", (147, 183, 469))
    assert hit and hit["label"].startswith("Škoda Octavia")


def test_marker_name_and_refine_in_parking():
    txt = "Hned to ověřím. [[CAR:147,183,469|Škoda Octavia Combi 2021]]"
    assert pk.parse_marker(txt) == (147, 183, 469)
    assert pk.marker_name(txt) == "Škoda Octavia Combi 2021"
    assert pk.marker_name("[[CAR:147,183,469]]") == "" and pk.parse_marker("[[CAR:147,183,469]]") == (147, 183, 469)
    assert "[[" not in pk.strip_marker(txt)
    car, label = pk.refine(txt, (147, 183, 469))
    assert label.startswith("Škoda Octavia") and car[2] in (469, 470)
    assert pk.refine("[[CAR:150,180,450|Neznámá Značka]]", (150, 180, 450)) == ((150, 180, 450), "")
    assert pk.refine("bez značky", (150, 180, 450)) == ((150, 180, 450), "")


def test_verdict_block_says_where_dimensions_come_from():
    hotel = {"parking_places": [{"name": "Garáž", "type": "garage", "max_height": "190 cm"}]}
    plain = pk.verdict_block(hotel, (147, 183, 469))
    assert "které jsi uvedla" in plain and "databáze" not in plain
    db = pk.verdict_block(hotel, (147, 183, 470), "Škoda Octavia Combi")
    assert "z databáze údajů výrobců pro Škoda Octavia Combi" in db and "PRÁVĚ tyto" in db
