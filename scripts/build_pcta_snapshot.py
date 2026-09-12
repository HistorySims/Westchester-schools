"""Build the Port Chester CBA snapshot: Appendices A and B, transcribed.

`PCTA_Contract_20232027.pdf` — Port Chester-Rye UFSD and the Port Chester
Teachers Association, 2023-24 through 2026-27, in term to 2027-06-30 — is a
56-page SCAN. `extract_pdf` returns 0 characters and 0 tables for it, so every
figure in it is invisible to the normal ingest path.

The repo's answer to a document the pipeline cannot reach is the snapshot:
transcribe where the transcription can happen, commit the result, expand it on
a runner into the same raw store and manifest a live crawl would have left.
`policy-boarddocs` and `agendas` do this for the IP-blocked BoardDocs crawls;
this does it for a scan with no fetchable URL, which `ocr --engine vision`
cannot reach either because that needs the PDF in a scrape artifact.

Transcribed from the page images at 150 dpi — the same job the vision engine
does on a runner — and emitted as FOUR documents so a citation names the
appendix it came from:

  A-teachers   printed 26-29  4 grids, 7 lanes x 28 steps x 4 school years
  A-assistants printed 30-33  4 grids, 9 tracks x 30 steps x 4 school years
  A-addenda    printed 34     the Level -> lane mapping, service increments
  B            printed 35-41  154 stipend positions + the hourly/per-event rates

**The salary cells pass the extractor's own audit invariants** (`audit_salary`):
monotonic within each lane as the step rises, canonical lane ordering at equal
step, year-over-year non-decreasing per cell, and every value inside its unit's
sanity band. 1,864 cells, zero violations. That is what makes a hand
transcription defensible; a transposed digit almost always breaks one of them.

Two details that are easy to get wrong and expensive to get wrong:

* The teacher lane header carries a second printed row of "Level" numbers
  (BA = 1, MA = 6, MA+30 = 8, MA+45 = 9, MA+60 = 10, MA+90 = 11,
  Doctorate = 12; Appendix A Addenda spells each one out). It is recorded as
  prose under each table, never as a header row: as a row it reads as a second
  step axis, and as a suffix on the column header ("BA (Level 1)")
  `normalize_lane` parses it into a credit count and invents a lane.
* Appendix B amounts keep their printed "(2)" / "(4)" suffixes. Those are the
  NUMBER OF POSITIONS at that rate, not part of the figure, and the extractor's
  `_num` already takes the leading number. Dropping them would lose contract
  meaning that nothing else records.

Regenerate:  uv run python scripts/build_pcta_snapshot.py
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

OUT = Path("data/snapshots/pcta-contract-2023-2027.jsonl.gz")
DISTRICT = "port-chester-rye"

# No public URL for this file was ever confirmed: the operator supplied it, and
# the search engines conflate at least four "PCTA"s. A guessed https:// link
# would put a dead link in every citation these schedules produce, so the
# provenance is stated as a URN instead. If the real URL turns up, ingest the
# document from it and let these fall out of the manifest.
URN = "urn:westchester-schools:ocr:PCTA_Contract_20232027.pdf"
SOURCE_PDF_SHA256 = "ce3903f1c8c7bd4b4de3289c1fca2245e9c45f2ffcb0840d21d13441fd71329d"

LANES = ["BA", "MA", "MA+30", "MA+45", "MA+60", "MA+90", "Doctorate"]
LEVELS = [1, 6, 8, 9, 10, 11, 12]          # the contract's own "Level" row
PAGES = {"2023-24": 26, "2024-25": 27, "2025-26": 28, "2026-27": 29}

GRIDS = {
"2023-24": [
(1,62731,72076,77942,80987,84055,90659,93966),
(2,63923,73446,79423,82526,85653,92381,95751),
(3,65137,74842,80932,84093,87280,94136,97570),
(4,66375,76264,82470,85691,88938,95924,99424),
(5,67636,77714,84037,87319,90629,97746,101313),
(6,68921,79191,85633,88979,92351,99603,103238),
(7,70231,80695,87261,90669,94106,101495,105199),
(8,71565,82229,88919,92392,95894,103423,107198),
(9,72925,83792,90608,94147,97716,105389,109235),
(10,74310,85384,92329,95936,99573,107391,111310),
(11,75723,87006,94083,97759,101465,109431,113425),
(12,77161,88659,95871,99616,103393,111511,115579),
(13,78627,90343,97693,101510,105358,113630,117775),
(14,80121,92060,99550,103439,107359,115788,120014),
(15,81643,93809,101441,105404,109398,117989,122294),
(16,83194,95591,103368,107406,111477,120230,124618),
(17,84775,97407,105332,109447,113595,122514,126986),
(18,86386,99258,107333,111526,115752,124842,129398),
(19,88028,101144,109373,113645,117951,127214,131856),
(20,89701,103066,111451,115804,120193,129632,134361),
(21,91405,105024,113569,118004,122476,132094,136915),
(22,93142,107019,115727,120246,124803,134605,139517),
(23,94911,109052,117926,122531,127174,137162,142168),
(24,96715,111125,120166,124859,129590,139768,144870),
(25,98552,113236,122449,127231,132053,142423,147622),
(26,100425,115388,124776,129648,134562,145129,150426),
(27,102334,117581,127147,132112,137119,147886,153285),
(28,104278,119815,129563,134622,139724,150696,156197)],
"2024-25": [
(1,64613,74238,80280,83417,86577,93379,96785),
(2,65841,75649,81806,85002,88223,95152,98624),
(3,67091,77087,83360,86616,89898,96960,100497),
(4,68366,78552,84944,88262,91606,98802,102407),
(5,69665,80045,86558,89939,93348,100678,104352),
(6,70989,81567,88202,91648,95122,102591,106335),
(7,72338,83116,89879,93389,96929,104540,108355),
(8,73712,84696,91587,95164,98771,106526,110414),
(9,75113,86306,93326,96971,100647,108551,112512),
(10,76539,87946,95099,98814,102560,110613,114649),
(11,77995,89616,96905,100692,104509,112714,116828),
(12,79476,91319,98747,102604,106495,114856,119046),
(13,80986,93053,100624,104555,108519,117039,121308),
(14,82525,94822,102537,106542,110580,119262,123614),
(15,84092,96623,104484,108566,112680,121529,125963),
(16,85690,98459,106469,110628,114821,123837,128357),
(17,87318,100329,108492,112730,117003,126189,130796),
(18,88978,102236,110553,114872,119225,128587,133280),
(19,90669,104178,112654,117054,121490,131030,135812),
(20,92392,106158,114795,119278,123799,133521,138392),
(21,94147,108175,116976,121544,126150,136057,141022),
(22,95936,110230,119199,123853,128547,138643,143703),
(23,97758,112324,121464,126207,130989,141277,146433),
(24,99616,114459,123771,128605,133478,143961,149216),
(25,101509,116633,126122,131048,136015,146696,152051),
(26,103438,118850,128519,133537,138599,149483,154939),
(27,105404,121108,130961,136075,141233,152323,157884),
(28,107406,123409,133450,138661,143916,155217,160883)],
"2025-26": [
(1,66551,76465,82688,85920,89174,96180,99689),
(2,67816,77918,84260,87552,90870,98007,101583),
(3,69104,79400,85861,89214,92595,99869,103512),
(4,70417,80909,87492,90910,94354,101766,105479),
(5,71755,82446,89155,92637,96148,103698,107483),
(6,73119,84014,90848,94397,97976,105669,109525),
(7,74508,85609,92575,96191,99837,107676,111606),
(8,75923,87237,94335,98019,101734,109722,113726),
(9,77366,88895,96126,99880,103666,111808,115887),
(10,78835,90584,97952,101778,105637,113931,118088),
(11,80335,92304,99812,103713,107644,116095,120333),
(12,81860,94059,101709,105682,109690,118302,122617),
(13,83416,95845,103643,107692,111775,120550,124947),
(14,85001,97667,105613,109738,113897,122840,127322),
(15,86615,99522,107619,111823,116060,125175,129742),
(16,88261,101413,109663,113947,118266,127552,132208),
(17,89938,103339,111747,116112,120513,129975,134720),
(18,91647,105303,113870,118318,122802,132445,137278),
(19,93389,107303,116034,120566,125135,134961,139886),
(20,95164,109343,118239,122856,127513,137527,142544),
(21,96971,111420,120485,125190,129935,140139,145253),
(22,98814,113537,122775,127569,132403,142802,148014),
(23,100691,115694,125108,129993,134919,145515,150826),
(24,102604,117893,127484,132463,137482,148280,153692),
(25,104554,120132,129906,134979,140095,151097,156613),
(26,106541,122416,132375,137543,142757,153967,159587),
(27,108566,124741,134890,140157,145470,156893,162621),
(28,110628,127111,137454,142821,148233,159874,165709)],
"2026-27": [
(1,68548,78759,85169,88498,91849,99065,102680),
(2,69850,80256,86788,90179,93596,100947,104630),
(3,71177,81782,88437,91890,95373,102865,106617),
(4,72530,83336,90117,93637,97185,104819,108643),
(5,73908,84919,91830,95416,99032,106809,110707),
(6,75313,86534,93573,97229,100915,108839,112811),
(7,76743,88177,95352,99077,102832,110906,114954),
(8,78201,89854,97165,100960,104786,113014,117138),
(9,79687,91562,99010,102876,106776,115162,119364),
(10,81200,93302,100891,104831,108806,117349,121631),
(11,82745,95073,102806,106824,110873,119578,123943),
(12,84316,96881,104760,108852,112981,121851,126296),
(13,85918,98720,106752,110923,115128,124167,128695),
(14,87551,100597,108781,113030,117314,126525,131142),
(15,89213,102508,110848,115178,119542,128930,133634),
(16,90909,104455,112953,117365,121814,131379,136174),
(17,92636,106439,115099,119595,124128,133874,138762),
(18,94396,108462,117286,121868,126486,136418,141396),
(19,96191,110522,119515,124183,128889,139010,144083),
(20,98019,112623,121786,126542,131338,141653,146820),
(21,99880,114763,124100,128946,133833,144343,149611),
(22,101778,116943,126458,131396,136375,147086,152454),
(23,103712,119165,128861,133893,138967,149880,155351),
(24,105682,121430,131309,136437,141606,152728,158303),
(25,107691,123736,133803,139028,144298,155630,161311),
(26,109737,126088,136346,141669,147040,158586,164375),
(27,111823,128483,138937,144362,149834,161600,167500),
(28,113947,130924,141578,147106,152680,164670,170680)],
}


# Teaching Assistants, PCTA Appendix A, printed pages 30-33 (PDF 32-35).
# Tracks: hours per day x credential.
TA_TRACKS = ["TA51", "TA52", "TA53", "TA61", "TA62", "TA63", "TA71", "TA72", "TA73"]
TA_TRACK_LABELS = {
    "TA51": "5 Hour Less than Associate", "TA52": "5 Hour Associate/60 Credits",
    "TA53": "5 Hour at least B.A.", "TA61": "6 Hour Less than Associate",
    "TA62": "6 Hour Associate/60 Credits", "TA63": "6 Hour at least B.A.",
    "TA71": "7 Hour Less than Associate", "TA72": "7 Hour Associate/60 Credits",
    "TA73": "7 Hour at least B.A.",
}
TA_PAGES = {"2023-24": 30, "2024-25": 31, "2025-26": 32, "2026-27": 33}
TA_GRIDS = {
"2023-24": [
(1,25511,27227,29058,30406,32464,34661,35301,37700,40265),
(2,25996,27744,29610,30983,33081,35319,35972,38416,41030),
(3,26491,28271,30173,31573,33709,35990,36656,39146,41810),
(4,26994,28809,30747,32172,34349,36674,37352,39890,42604),
(5,27507,29356,31331,32783,35002,37371,38062,40648,43413),
(6,28029,29914,31926,33406,35668,38081,38785,41420,44239),
(7,28562,30483,32533,34040,36346,38804,39521,42207,45079),
(8,29105,31062,33151,34687,37036,39542,40272,43010,45936),
(9,29658,31652,33781,35347,37739,40293,41037,43827,46808),
(10,30221,32253,34423,36018,38456,41058,41817,44659,47697),
(11,30795,32866,35077,36702,39186,41838,42611,45507,48604),
(12,31380,33490,35743,37399,39931,42633,43421,46372,49528),
(13,31976,34127,36422,38110,40690,43442,44246,47252,50469),
(14,32584,34776,37114,38834,41464,44267,45086,48150,51428),
(15,33203,35436,37820,39572,42252,45109,45943,49065,52405),
(16,33833,36110,38538,40323,43054,45966,46816,49997,53401),
(17,34476,36796,39271,41090,43872,46839,47705,50947,54416),
(18,35131,37495,40017,41871,44705,47729,48612,51915,55450),
(19,35799,38208,40777,42666,45555,48636,49536,52902,56504),
(20,36478,38934,41551,43476,46420,49559,50477,53907,57577),
(21,37172,39674,42340,44302,47302,50501,51436,54931,58671),
(22,37878,40428,43145,45144,48201,51461,52414,55974,59785),
(23,38598,41196,43965,46002,49117,52438,53410,57038,60921),
(24,39332,41979,44800,46876,50050,53434,54424,58122,62079),
(25,40079,42776,45651,47767,51000,54450,55458,59226,63258),
(26,40841,43589,46518,48675,51970,55484,56512,60352,64460),
(27,41616,44417,47402,49600,52957,56538,57585,61498,65685),
(28,42407,45260,48302,50542,53964,57612,58679,62666,66934),
(29,43213,46120,49220,51502,54989,58707,59794,63857,68206),
(30,44034,46997,50155,52481,56033,59822,60930,65070,69501)],
"2024-25": [
(1,26276,28044,29930,31318,33438,35701,36360,38831,41473),
(2,26776,28576,30498,31912,34073,36379,37051,39568,42261),
(3,27286,29119,31078,32520,34720,37070,37756,40320,43064),
(4,27804,29673,31669,33137,35379,37774,38473,41087,43882),
(5,28332,30237,32271,33766,36052,38492,39204,41867,44715),
(6,28870,30811,32884,34408,36738,39223,39949,42663,45566),
(7,29419,31397,33509,35061,37436,39968,40707,43473,46431),
(8,29978,31994,34146,35728,38147,40728,41480,44300,47314),
(9,30548,32602,34794,36407,38871,41502,42268,45142,48212),
(10,31128,33221,35456,37099,39610,42290,43072,45999,49128),
(11,31719,33852,36129,37803,40362,43093,43889,46872,50062),
(12,32321,34495,36815,38521,41129,43912,44724,47763,51014),
(13,32935,35151,37515,39253,41911,44745,45573,48670,51983),
(14,33562,35819,38227,39999,42708,45595,46439,49595,52971),
(15,34199,36499,38955,40759,43520,46462,47321,50537,53977),
(16,34848,37193,39694,41533,44346,47345,48220,51497,55003),
(17,35510,37900,40449,42323,45188,48244,49136,52475,56048),
(18,36185,38620,41218,43127,46046,49161,50070,53472,57114),
(19,36873,39354,42000,43946,46922,50095,51022,54489,58199),
(20,37572,40102,42798,44780,47813,51046,51991,55524,59304),
(21,38287,40864,43610,45631,48721,52016,52979,56579,60431),
(22,39014,41641,44439,46498,49647,53005,53986,57653,61579),
(23,39756,42432,45284,47382,50591,54011,55012,58749,62749),
(24,40512,43238,46144,48282,51552,55037,56057,59866,63941),
(25,41281,44059,47021,49200,52530,56084,57122,61003,65156),
(26,42066,44897,47914,50135,53529,57149,58207,62163,66394),
(27,42864,45750,48824,51088,54546,58234,59313,63343,67656),
(28,43679,46618,49751,52058,55583,59340,60439,64546,68942),
(29,44509,47504,50697,53047,56639,60468,61588,65773,70252),
(30,45355,48407,51660,54055,57714,61617,62758,67022,71586)],
"2025-26": [
(1,27064,28885,30828,32258,34441,36772,37451,39996,42717),
(2,27579,29433,31413,32869,35095,37470,38163,40755,43529),
(3,28105,29993,32010,33496,35762,38182,38889,41530,44356),
(4,28638,30563,32619,34131,36440,38907,39627,42320,45198),
(5,29182,31144,33239,34779,37134,39647,40380,43123,46056),
(6,29736,31735,33871,35440,37840,40400,41147,43943,46933),
(7,30302,32339,34514,36113,38559,41167,41928,44777,47824),
(8,30877,32954,35170,36800,39291,41950,42724,45629,48733),
(9,31464,33580,35838,37499,40037,42747,43536,46496,49658),
(10,32062,34218,36520,38212,40798,43559,44364,47379,50602),
(11,32671,34868,37213,38937,41573,44386,45206,48278,51564),
(12,33291,35530,37919,39677,42363,45229,46066,49196,52544),
(13,33923,36206,38640,40431,43168,46087,46940,50130,53542),
(14,34569,36894,39374,41199,43989,46963,47832,51083,54560),
(15,35225,37594,40124,41982,44826,47856,48741,52053,55596),
(16,35893,38309,40885,42779,45676,48765,49667,53042,56653),
(17,36575,39037,41662,43593,46544,49691,50610,54049,57729),
(18,37271,39779,42455,44421,47427,50636,51572,55076,58827),
(19,37979,40535,43260,45264,48330,51598,52553,56124,59945),
(20,38699,41305,44082,46123,49247,52577,53551,57190,61083),
(21,39436,42090,44918,47000,50183,53576,54568,58276,62244),
(22,40184,42890,45772,47893,51136,54595,55606,59383,63426),
(23,40949,43705,46643,48803,52109,55631,56662,60511,64631),
(24,41727,44535,47528,49730,53099,56688,57739,61662,65859),
(25,42519,45381,48432,50676,54106,57767,58836,62833,67111),
(26,43328,46244,49351,51639,55135,58863,59953,64028,68386),
(27,44150,47123,50289,52621,56182,59981,61092,65243,69686),
(28,44989,48017,51244,53620,57250,61120,62252,66482,71010),
(29,45844,48929,52218,54638,58338,62282,63436,67746,72360),
(30,46716,49859,53210,55677,59445,63466,64641,69033,73734)],
"2026-27": [
(1,27876,29752,31753,33226,35474,37875,38575,41196,43999),
(2,28406,30316,32355,33855,36148,38594,39308,41978,44835),
(3,28948,30893,32970,34501,36835,39327,40056,42776,45687),
(4,29497,31480,33598,35155,37533,40074,40816,43590,46554),
(5,30057,32078,34236,35822,38248,40836,41591,44417,47438),
(6,30628,32687,34887,36503,38975,41612,42381,45261,48341),
(7,31211,33309,35549,37196,39716,42402,43186,46120,49259),
(8,31803,33943,36225,37904,40470,43209,44006,46998,50195),
(9,32408,34587,36913,38624,41238,44029,44842,47891,51148),
(10,33024,35245,37616,39358,42022,44866,45695,48800,52120),
(11,33651,35914,38329,40105,42820,45718,46562,49726,53111),
(12,34290,36596,39057,40867,43634,46586,47448,50672,54120),
(13,34941,37292,39799,41644,44463,47470,48348,51634,55148),
(14,35606,38001,40555,42435,45309,48372,49267,52615,56197),
(15,36282,38722,41328,43241,46171,49292,50203,53615,57264),
(16,36970,39458,42112,44062,47046,50228,51157,54633,58353),
(17,37672,40208,42912,44901,47940,51182,52128,55670,59461),
(18,38389,40972,43729,45754,48850,52155,53119,56728,60592),
(19,39118,41751,44558,46622,49780,53146,54130,57808,61743),
(20,39860,42544,45404,47507,50724,54154,55158,58906,62915),
(21,40619,43353,46266,48410,51688,55183,56205,60024,64111),
(22,41390,44177,47145,49330,52670,56233,57274,61164,65329),
(23,42177,45016,48042,50267,53672,57300,58362,62326,66570),
(24,42979,45871,48954,51222,54692,58389,59471,63512,67835),
(25,43795,46742,49885,52196,55729,59500,60601,64718,69124),
(26,44628,47631,50832,53188,56789,60629,61752,65949,70438),
(27,45475,48537,51798,54200,57867,61780,62925,67200,71777),
(28,46339,49458,52781,55229,58968,62954,64120,68476,73140),
(29,47219,50397,53785,56277,60088,64150,65339,69778,74531),
(30,48117,51355,54806,57347,61228,65370,66580,71104,75946)],
}

# Appendix B - Additional Compensation, printed pages 35-41 (PDF 37-43).
# Amounts kept EXACTLY as printed: a trailing "(2)" is the number of positions
# at that rate, not part of the figure, and dropping it loses contract meaning.
COACH_COLUMNS = ["Varsity Head Coach", "Varsity Assistant", "JV Head Coach",
                 "JV Assistant", "Modified Head Coach", "Modified Assistant Coach"]
# (tier, sport, *six columns)
COACHES = [
 (9, "Football", "10,300", "7,210 (4)", "7,210", "5,150", "5,665", "4,120"),
 (8, "Boys Basketball", "8,500", "5,950", "5,950", "", "4,675 (2)", ""),
 (8, "Girls Basketball", "8,500", "5,950", "5,950", "", "4,675 (2)", ""),
 (8, "Wrestling", "8,500", "5,950 (2)", "5,950", "4,250", "4,675", ""),
 (7, "Baseball", "7,800", "5,700 (2)", "5,460", "", "4,450 (2)", ""),
 (7, "Softball", "7,800", "5,700 (2)", "5,460", "", "4,450 (2)", ""),
 (7, "Boys Lacrosse", "7,800", "5,700", "5,460", "3,900", "4,450", "3,350"),
 (7, "Girls Lacrosse", "7,800", "5,700", "5,460", "3,900", "4,450", "3,350"),
 (6, "Girls Soccer", "6,600", "5,475", "4,960 (2)", "", "4,325 (2)", ""),
 (6, "Boys Soccer", "6,600", "5,475", "4,960 (2)", "", "4,325 (2)", ""),
 (6, "Girls Volleyball", "6,600", "5,475", "4,960", "", "4,325 (2)", ""),
 (6, "Boys Volleyball", "6,600", "5,475", "", "", "", ""),
 (6, "Girls Indoor Track", "6,600", "5,475", "", "", "", ""),
 (6, "Boys Indoor Track", "6,600", "5,475", "", "", "", ""),
 (6, "Girls Spring Track", "6,600", "5,475", "", "", "4,325", ""),
 (6, "Boys Spring Track", "6,600", "5,475", "", "", "4,325", "3,290"),
 (5, "Fall Cheerleading", "6,000", "4,320", "4,320", "3,300", "4,200", "3,200 (2)"),
 (5, "Winter Cheerleading", "6,000", "4,320", "4,320", "3,300", "4,200", "3,200"),
 (5, "Fall Strength & Conditioning", "6,000", "", "", "", "", ""),
 (5, "Winter Strength & Conditioning", "6,000", "", "", "", "", ""),
 (5, "Spring Strength & Conditioning", "6,000", "", "", "", "", ""),
 (5, "Varsity Girls Flag Football", "6,000", "4,320", "", "", "", ""),
 (4, "Bowling", "4,700", "", "", "", "", ""),
 (4, "Cross Country", "4,700", "4,015", "", "", "3,985", "3,150"),
 (4, "Girls Swimming", "4,700", "4,015", "", "", "", ""),
 (4, "Boys Swimming", "4,700", "4,015", "", "", "", ""),
 (4, "Summer Strength & Conditioning", "4,700", "", "", "", "", ""),
 (3, "Girls Tennis", "4,000", "", "3,275", "", "", ""),
 (3, "Boys Tennis", "4,000", "", "", "", "", ""),
 (2, "Golf", "3,000", "", "", "", "", ""),
 (1, "JFK Sports Intramurals", "1,700", "", "", "", "", ""),
 (1, "Edison Sports Intramurals", "1,700", "", "", "", "", ""),
 (1, "Park Ave Sports Intramurals", "1,700", "", "", "", "", ""),
 (1, "King St Sports Intramurals", "1,700", "", "", "", "", ""),
 (1, "Fall Season Technology, Media, Music, Electronics Specialist",
     "1,700", "", "", "", "", ""),
 (1, "Winter Season Technology, Media, Music Electronics Specialist",
     "1,700", "", "", "", "", ""),
 (1, "Spring Season Technology, Media, Music Electronics Specialist",
     "1,700", "", "", "", "", ""),
]
ATHLETIC_COORDINATORS = [
 ("Fall Season Athletic Coordinator", "5,310"),
 ("Winter Season Athletic Coordinator", "5,310"),
 ("Spring Season Athletic Coordinator", "5,310"),
]
COACH_LONGEVITY = [
 ("Unit members with 3 years longevity in position", "$150.00 increase"),
 ("Unit members with 5 years longevity in position", "$250.00 increase"),
 ("Unit members with 8 years longevity in position", "$350.00 increase"),
 ("Unit members with 10 years longevity in position", "$500.00 increase"),
]
CLUB_TIERS = [
 ("Tier 3", "1,751", [
  "Advisor - Junior Class", "Advisor - Senior Class",
  "Builders Club/Junior Key Club (MS)", "Key Club (HS)",
  "Byron Womack Mentoring Program (HS)", "Ram Page",
  "Student Council Advisor (MS)", "Student Senate Advisor (HS)"]),
 ("Tier 2", "1,056", [
  "Astro Club (MS)", "Career Cruisers Club (HS)", "Chess Club (MS)",
  "Co-Ed Rugby Club", "Co-Ed Volleyball Club", "Debate Club (MS)",
  "Film Club (HS)", "Model U.N.", "Newcomers' Club/Book Writing (MS)",
  "One World Youth Club (HS)", "Peer Tutoring Center (HS)",
  "Rainbow Alliance (MS)", "Reading Rams (MS)", "Table Top Adventurers (MS)"]),
 ("Tier 1", "876", [
  "Advisor - Freshmen Class", "Advisor - Sophomore Class", "Anime Club",
  "Asian Culture Club", "Badminton Club (HS)", "Chess Club (HS)",
  "Culture Club", "CODA Club (HS)", "Debate Club (HS)", "Debate Club (MS)",
  "Entrepreneurship Club (HS)", "Environmental Club (HS)", "Fitness Club (HS)",
  "French Club", "Gaming Club", "Gardening Club (HS)", "Global Leader Club (HS)",
  "GSA Advisor (HS)", "Habitat for Humanity Club (HS)",
  "International Thespian Honor Society (HS)",
  "International Thespian Honor Society (MS)", "National Junior Honor Society",
  "Literacy Book Club", "Mu Alpha Theta Math National Honor Society (HS)",
  "National Art Honor Society", "National English Honor Society",
  "National Honor Society", "Ping Pong Club (HS)", "Poetry Slam Club (HS)",
  "Ram Coding Alliance (HS)", "Royal Steppers", "Science Olympiad",
  "Science Research Club (HS)",
  "Rho-Kappa National Social Studies Honor Society", "Spanish Club (HS)",
  "African American Club (HS)", "Theology Club (HS)",
  "Tri-M Music Honor Society (MS)", "Tri-M Music Honor Society (HS)",
  "Varsity Club (HS)", "World Language Honor Society (HS)", "Italian Club (HS)"]),
]
FACILITATORS = [
 ("Planetarium Facilitator", "8,209"),
 ("Coordinator of Volunteer Services", "6,638"),
 ("Anthony Foust Coordinator", "6,638"),
 ("Science Coordinator Grades K-12", "6,639"),
 ("Ram Nation: Coordinator of Student Activities", "6,638"),
 ("ELA Coordinator 4-12", "4,647"),
 ("Chairperson for the Committee on Special Education", "6,639"),
 ("Subcommittee Chair (HS)", "2,655"),
 ("Subcommittee Chair (MS)", "2,655"),
 ("Subcommittee Chair - one position at each elementary school", "1,328"),
 ("Chairperson/Team Leader (9 or more people, including the Chair/TL)", "3,660"),
 ("Chairperson/Team Leader (8 or fewer people, including the Chair/TL)", "3,014"),
 ("Garden Coordinator", "3,983"),
 ("RTI Subcommittee Chairperson (12-3 per each elementary school)", "1,000"),
 ("IEP Team Facilitators", "2,000"),
 ("Assistive Technology Facilitators (2)", "2,000"),
 ("Food Pantry Coordinator", "4,000"),
]
OTHER_POSITIONS = [
 ("Peningian", "5,980"), ("School Counselors", "5,853"), ("Port Light", "3,984"),
 ("Memory Book (MS)", "3,320"), ("G.O. Fund (HS)", "3,013"),
 ("G.O. Fund (MS)", "3,013"), ("Tamarack Tower", "1,993"),
 ("Elementary Science Liaison", "3,635"), ("Elementary Science Assistant", "1,912"),
 ("Math Coach - per school", "2,323"),
 ("Learning Specialists Math, S.S., Science, ELA Academic, Specials Area", "1,226"),
]
BAND_MUSIC_DRAMA = [
 ("Band Director", "8,627"), ("High School Percussion Caption Head", "4,647"),
 ("Band Assistant", "4,451"),
 ("Drama (HS) - per performance (maximum of 2 productions per year)", "3,961"),
 ("Drama (MS)", "2,987"), ("Color Guard Director", "2,989"),
 ("Jazz Ensemble (HS)", "2,987"), ("Choral Director (HS)", "2,987"),
 ("Band Stage (MS)", "1,645"), ("Select Band (MS)", "1,645"),
 ("Pit Orchestra", "1,645"), ("Rock Band (HS)", "1,494"),
 ("Show Choir (MS)", "1,329"), ("Show Choir (HS)", "1,329"),
 ("Orchestra Club", "1,329"), ("Band Director - 2 parades (6th grade)", "1,002"),
 ("Band Middle School - 2 parades", "1,002"),
 ("Band 5th grade parade director - Memorial Day Parade", "487"),
]
TECHNOLOGY = [
 ("Cable TV Production", "5,857"), ("Cable TV Programmer", "2,727"),
 ("Website Coordinator", "2,417"), ("Computer Liaison", "2,417"),
]
HOURLY_RATE = [
 ("July 1, 2023 - June 30, 2024", "$55.00 per hour"),
 ("July 1, 2024 - June 30, 2025", "$55.00 per hour"),
 ("July 1, 2025 - June 30, 2026", "$60.00 per hour"),
 ("July 1, 2026 - June 30, 2027", "$60.00 per hour"),
]


# ---- prose (Appendix A Addenda, printed 34; Appendix B tail, printed 40-41) --

ADDENDA = """\
<h2>Appendix A - Addenda (printed page 34)</h2>
<ol>
<li>Level 1 shall apply to teachers who hold a Life Teaching Certificate or a
valid Teaching Certificate and a Baccalaureate Degree.</li>
<li>Level 6 shall apply to teachers who hold a Master's Degree and valid
Teaching Certificate.</li>
<li>Level 8 shall apply to teachers who hold a Master's Degree and a valid
Teaching Certificate and shall have completed thirty (30) semester hours of
approved graduate study subsequent to the receipt of the Master's Degree.</li>
<li>Level 9 shall apply to teachers who hold a Master's Degree and a valid
Teaching Certificate and shall have completed forty-five (45) semester hours of
approved graduate study subsequent to the receipt of the Master's Degree.
Effective July 1, 1998 no teacher shall be placed on Level 9 (MA+45). Any
teacher currently on Level 9 shall continue to be compensated at that Level and
teachers who have earned credit for this Level prior to September 1, 1998 shall
be compensated and continue to be compensated at that Level. Existing staff
shall have a two (2) year 'grace period' (from July 1, 1998 to June 30, 2000)
in which to reach the MA+45 Level.</li>
<li>Level 10 shall apply to teachers who have a Master's Degree and a valid
Teaching Certificate and shall have completed sixty (60) semester hours of
approved graduate study subsequent to the receipt of the Master's Degree.</li>
<li>Level 11 shall apply to teachers who have a Master's Degree and a valid
Teaching Certificate and shall have completed ninety (90) semester hours of
approved graduate study subsequent to the receipt of the Master's Degree.</li>
<li>Level 12 shall apply to teachers who have been granted a Doctoral Degree and
who hold a valid Teaching Certificate.</li>
<li>Previous teaching experience of new appointees shall be evaluated by the
Superintendent of Schools. Upon the recommendation of the Superintendent of
Schools, and with the approval of the Board of Education, the new appointee
shall be placed at the time of appointment on the step of the salary schedule
reflecting the evaluated credit as allowed.</li>
<li>Advancement to the next step of the salary schedule shall be effected as
follows: A bargaining unit member hired between September 1 and January 31, will
move to the next salary step on September 1 of the following school year. A
bargaining unit member hired between February 1 and June 30, will move to the
next salary step on February 1 of the next school year. The anniversary dates
for bargaining unit members currently on staff will be adjusted to conform to
the above paragraph.</li>
<li>Effective July 1, 2016, the service increments below shall be cumulative and
all affected unit members shall have their service increments adjusted to
cumulative status as of said date. A service increment of $750 shall be granted
to all unit members who shall have completed fifteen (15) years of full-time
teaching service in the Port Chester Public Schools. A service increment of
$1,250 shall be granted to all bargaining unit members who have completed twenty
(20) years of full-time teaching service. A service increment of $2,500 shall be
granted to all bargaining unit members who have completed twenty-five (25) years
of full-time teaching service. A service increment of $3,500 shall be granted to
all bargaining unit members who have completed 30 years of full-time teaching
service.</li>
<li>Any individual hired as a long-term substitute, meaning that he or she
serves in the same assignment for at least one semester shall be compensated by
placement on a step/level on the teacher salary schedule.</li>
</ol>
"""

APPENDIX_B_TAIL = """\
<h2>Compensation exclusive of the salary schedule (printed pages 40-41)</h2>
<p>All differentials paid shall not be considered as a part of the base salary
for the purposes of computing salary increases.</p>
<p>Instructional/tutorial service, summer school remuneration, curriculum
writing, and training services provided by certified teachers are paid at the
hourly rates in the table above.</p>
<p>Bargaining unit members scheduled in more than one school in a given day are
reimbursed at the IRS mileage rate, computed from the contract's table of
distances between schools and claimed semi-annually in December and June.</p>
<p>Breakfast duty shall be remunerated at $30 per day for the life of the
Agreement.</p>
<p>Effective July 1, 2023, Middle School and High School teachers accepting a
sixth (6th) teaching period per day shall receive additional compensation of
$8,000 per annum and $4,000 a school year for a half-year course.</p>
<p>Chaperoning - payment of $75.00 per event. If chaperoning entails an
overnight trip, then the payment shall be $80.00 per night up to a maximum of
$240.00. Chaperoning payments shall not apply to individuals who receive a
stipend for this same activity. Eligible events: NYSSMA and All County music
events; school plays and concerts, parades and dances; school-supported Middle
School trips to Boston, DC and overseas; the D.A.R.E. Breakfast on Sunday; and
field trips entailing more than two (2) hours outside the regular school
day.</p>
<p>Any unit member that provides athletic event supervision shall be paid
$75.00 per event, and any unit member that provides supervision at an athletic
tournament shall be paid $50.00 per hour.</p>
<p>Retirement Early Notice Provision - a payment of $2,000 to any unit member
who provides an irrevocable letter of resignation for retirement purposes on or
before March 1 of the calendar year of retirement, for a retirement effective
between June 30 and August 31 of that year, paid within 30 calendar days of
retirement as a non-elective employer contribution into the member's IRC section
403(b) account without a cash option, in addition to any retirement
incentive.</p>
<p>Coaching longevity increases (effective July 1, 2020) are pro-rated for split
stipends and are NOT cumulative; years coaching prior to July 1, 2019 count
toward them, and "position" means any coaching appointment within the same sport
per season.</p>
"""


# ---- rendering ---------------------------------------------------------

def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["<table>", "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return out


def _pairs_table(caption: str, pairs, amount_header: str = "Stipend") -> list[str]:
    return ([f"<h2>{caption}</h2>"]
            + _table(["Position", amount_header], [[p, a] for p, a in pairs]))


def teacher_html() -> str:
    levels = ", ".join(f"{lane} = Level {lvl}" for lane, lvl in zip(LANES, LEVELS))
    out = ["<h1>Port Chester-Rye UFSD and the Port Chester Teachers Association - "
           "Appendix A, Teacher Salary Schedules</h1>",
           "<p>Contract school years 2023-2024, 2024-2025, 2025-2026 and "
           "2026-2027.</p>"]
    for year, grid in GRIDS.items():
        out.append(f"<h2>Teachers - {year.replace('-', '/')} School Year "
                   f"(Appendix A, printed page {PAGES[year]})</h2>")
        out += _table(["Step"] + LANES,
                      [[str(r[0])] + [f"{v:,}" for v in r[1:]] for r in grid])
        out.append(f"<p>Lane levels as printed: {levels}.</p>")
    return "\n".join(out) + "\n"


def assistant_html() -> str:
    tracks = ", ".join(f"{t} = {TA_TRACK_LABELS[t]}" for t in TA_TRACKS)
    out = ["<h1>Port Chester-Rye UFSD and the Port Chester Teachers Association - "
           "Appendix A, Teaching Assistant Salary Schedules</h1>",
           "<p>Teaching assistants are paid by scheduled hours per day (5, 6 or 7) "
           "crossed with credential, so these tracks are job classifications, not "
           "education lanes.</p>"]
    for year, grid in TA_GRIDS.items():
        out.append(f"<h2>Teaching Assistants - {year.replace('-', '/')} School Year "
                   f"(Appendix A, printed page {TA_PAGES[year]})</h2>")
        out += _table(["Step"] + TA_TRACKS,
                      [[str(r[0])] + [f"{v:,}" for v in r[1:]] for r in grid])
        out.append(f"<p>Tracks as printed: {tracks}.</p>")
    return "\n".join(out) + "\n"


def addenda_html() -> str:
    return ("<h1>Port Chester-Rye UFSD and the Port Chester Teachers Association - "
            "Appendix A Addenda</h1>\n" + ADDENDA)


def appendix_b_html() -> str:
    out = ["<h1>Port Chester-Rye UFSD and the Port Chester Teachers Association - "
           "Appendix B, Additional Compensation</h1>",
           "<h2>Coaches Tiers (printed pages 35-36)</h2>"]
    out += _table(["Tier", "Sport"] + COACH_COLUMNS,
                  [[str(c[0]), c[1], *c[2:]] for c in COACHES])
    out.append("<p>A parenthesised number after an amount is the number of "
               "positions paid at that rate.</p>")
    out += _pairs_table("Athletic Coordinators (printed page 36)",
                        ATHLETIC_COORDINATORS)
    out.append("<h2>Coaching longevity increases (printed page 37)</h2>")
    out += _table(["Longevity in position", "Increase"],
                  [[a, b] for a, b in COACH_LONGEVITY])
    out.append("<h2>Facilitators/Coordinators - Clubs/Advisors Tiers "
               "(printed page 38)</h2>")
    rows = [[tier, amount, name]
            for tier, amount, names in CLUB_TIERS for name in names]
    out += _table(["Tier", "Stipend", "Club/Advisor position"], rows)
    out += _pairs_table("Facilitators and Coordinators (printed page 39)",
                        FACILITATORS)
    out += _pairs_table("Other Positions (printed page 39)", OTHER_POSITIONS)
    out.append("<p>School counselors, who receive a contractual stipend, will "
               "work until 4:00 p.m. Monday through Thursday, September 1 "
               "through June 30.</p>")
    out += _pairs_table("Band/Music/Drama (printed page 39)", BAND_MUSIC_DRAMA)
    out += _pairs_table("Technology (printed page 40)", TECHNOLOGY)
    out.append("<h2>Hourly rate for instructional, summer school, curriculum "
               "writing and training service (printed page 40)</h2>")
    out += _table(["Period", "Rate"], [[a, b] for a, b in HOURLY_RATE])
    out.append(APPENDIX_B_TAIL)
    return "\n".join(out) + "\n"


DOCUMENTS = [
    ("appendix-a-teachers",
     "PCTA Contract 2023-2027 - Appendix A Teacher Salary Schedules (OCR)",
     teacher_html),
    ("appendix-a-teaching-assistants",
     "PCTA Contract 2023-2027 - Appendix A Teaching Assistant Salary Schedules (OCR)",
     assistant_html),
    ("appendix-a-addenda",
     "PCTA Contract 2023-2027 - Appendix A Addenda (OCR)",
     addenda_html),
    ("appendix-b",
     "PCTA Contract 2023-2027 - Appendix B Additional Compensation (OCR)",
     appendix_b_html),
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        for fragment, title, render in DOCUMENTS:
            html = render()
            total += len(html.encode("utf-8"))
            fh.write(json.dumps({
                "district": DISTRICT,
                "doc_type": "contract",
                "title": title,
                "source_url": f"{URN}#{fragment}",
                "date": None,
                "source_pdf_sha256": SOURCE_PDF_SHA256,
                "transcription": "manual-vision-ocr",
                "html": html,
            }, ensure_ascii=False) + "\n")
            print(f"  {fragment:34s} {len(html):>7,} bytes")
    print(f"{OUT}: {len(DOCUMENTS)} records, {total:,} bytes html "
          f"-> {OUT.stat().st_size:,} bytes gzipped")


if __name__ == "__main__":
    main()
