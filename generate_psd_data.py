"""
generate_psd_data.py
--------------------
Run this script from the PSD_database folder (or double-click it).
It will scan all PDF filenames, extract drug name + PBAC meeting date,
classify by therapy area, and write psd_data.js for the dashboard.

Usage:
    python generate_psd_data.py

Output:
    psd_data.js  (in the same folder — load psd_dashboard.html to view)
"""

import os
import re
import json
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MONTH_MAP = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}

# --- Therapy area keyword classification ---
THERAPY_KEYWORDS = {
    'Oncology': [
        'abemaciclib','abiraterone','acalabrutinib','afatinib','aflibercept','alectinib',
        'alemtuzumab','alpelisib','atezolizumab','avapritinib','axitinib','bendamustine',
        'bevacizumab','binimetinib','blinatumomab','bortezomib','bosutinib','brentuximab',
        'brigatinib','cabazitaxel','cabozantinib','capmatinib','carfilzomib','ceritinib',
        'cetuximab','cobimetinib','copanlisib','crizotinib','dabrafenib','dacomitinib',
        'daratumumab','dasatinib','durvalumab','elotuzumab','encorafenib','entrectinib',
        'enzalutamide','erdafitinib','erlotinib','everolimus','fulvestrant','gefitinib',
        'gilteritinib','glasdegib','ibrutinib','idelalisib','imatinib','ipilimumab',
        'isatuximab','ixazomib','lapatinib','larotrectinib','lenalidomide','lorlatinib',
        'luspatercept','midostaurin','mobocertinib','neratinib','nilotinib','nivolumab',
        'niraparib','obinutuzumab','olaparib','osimertinib','palbociclib','panitumumab',
        'pazopanib','pembrolizumab','pemigatinib','pertuzumab','ponatinib','pralsetinib',
        'ramucirumab','regorafenib','ribociclib','rucaparib','ruxolitinib','selumetinib',
        'selpercatinib','sotorasib','sunitinib','talazoparib','temozolomide','trastuzumab',
        'tucatinib','vandetanib','vemurafenib','venetoclax','vismodegib','zanubrutinib',
        'zoledronic','ivosidenib','epcoritamab','tivozanib','sacituzumab','loncastuximab',
        'tremelimumab','tebentafusp','relatlimab','mirvetuximab','elranatamab',
    ],
    'Immunology / Rheumatology': [
        'abatacept','adalimumab','baricitinib','belimumab','brodalumab','certolizumab',
        'dupilumab','etanercept','filgotinib','golimumab','guselkumab','ixekizumab',
        'infliximab','inebilizumab','leflunomide','mepolizumab','natalizumab',
        'ocrelizumab','ofatumumab','ozanimod','ponesimod','risankizumab','rituximab',
        'sarilumab','secukinumab','siponimod','tezepelumab','tocilizumab','tofacitinib',
        'upadacitinib','ustekinumab','vedolizumab','bimekizumab','mirikizumab',
        'spesolimab','imsidolimab','deucravacitinib','izokibep',
    ],
    'Rare Disease': [
        'agalsidase','alglucosidase','amifampridine','amino-acid','asfotase','avalglucosidase',
        'burosumab','cerliponase','darvadstrocel','eculizumab','eliglustat','emapalumab',
        'idursulfase','imiglucerase','laronidase','lumasiran','maralixibat','mecasermin',
        'migalastat','nusinersen','onasemnogene','ravulizumab','risdiplam','somatrogon',
        'somatropin','stiripentol','taliglucerase','tezacaftor','ivacaftor','elexacaftor',
        'vosoritide','ataluren','avalglucosidase','brineura','cerdelga','fabrazyme',
        'myozyme','naglazyme','replagal','strensiq','vimizim','aldurazyme',
    ],
    'Cardiovascular': [
        'alirocumab','evolocumab','ivabradine','sacubitril','vericiguat','cangrelor',
        'edoxaban','apixaban','rivaroxaban','dabigatran','ticagrelor','prasugrel',
        'inclisiran','bempedoic','icosapent',
    ],
    'Neurology': [
        'alemtuzumab','amantadine','botulinum','dimethyl','fingolimod','glatiramer',
        'interferon','natalizumab','ocrelizumab','ofatumumab','ozanimod','perampanel',
        'ponesimod','siponimod','valbenazine','teriflunomide','cladribine',
        'eptinezumab','erenumab','fremanezumab','galcanezumab','lasmiditan',
        'rimegepant','ubrogepant','atogepant',
    ],
    'Diabetes / Endocrine': [
        'alogliptin','canagliflozin','dapagliflozin','dulaglutide','empagliflozin',
        'exenatide','liraglutide','semaglutide','sitagliptin','tirzepatide',
        'saxagliptin','linagliptin','insulin','glargine','degludec','detemir',
        'ertugliflozin','sotagliflozin',
    ],
    'Respiratory': [
        'aclidinium','benralizumab','budesonide','dupilumab','indacaterol','mepolizumab',
        'omalizumab','reslizumab','roflumilast','tezepelumab','tiotropium',
        'umeclidinium','glycopyrronium','vilanterol','eformoterol','salmeterol',
        'fluticasone','beclomethasone','ciclesonide',
    ],
    'Infectious Disease': [
        'aciclovir','nirmatrelvir','ritonavir','molnupiravir','remdesivir',
        'sotrovimab','tixagevimab','cilgavimab','letermovir','maribavir',
        'baloxavir','oseltamivir','valganciclovir','dolutegravir','bictegravir',
        'cabotegravir','rilpivirine','lenacapavir','islatravir','fostemsavir',
        'ibalizumab','hepatitis','hep-c','hepc','sofosbuvir','glecaprevir',
        'pibrentasvir','velpatasvir','ledipasvir',
    ],
}


def parse_date_from_filename(fname):
    """Extract (year, month) from a PSD filename. Returns (None, None) on failure."""
    s = fname.lower().replace('%20', '-').replace(' ', '-').replace('.pdf', '')
    s = re.sub(r'\.docx', '', s)

    # Pattern: -psd-<month_name>-<4digit_year>
    m = re.search(r'-psd[^-]*-([a-z]+)-(\d{4})', s)
    if m:
        mo = MONTH_MAP.get(m.group(1))
        yr = int(m.group(2))
        if mo and 2000 <= yr <= 2030:
            return yr, mo

    # Pattern: -psd-<MM>-<YYYY>  (numeric month)
    m = re.search(r'-psd[^-]*-(\d{1,2})-(\d{4})', s)
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 2000 <= yr <= 2030:
            return yr, mo

    # Pattern: -psd-<YYYY> (year only, no month)
    m = re.search(r'-psd[^-]*-(\d{4})', s)
    if m:
        yr = int(m.group(1))
        if 2000 <= yr <= 2030:
            return yr, None

    # Old-style: drug-name-psd-M-D-YYYY-MM-FINAL (e.g. alendronate-...-2011-07-final)
    m = re.search(r'(\d{4})-(\d{2})(?:-final)?$', s)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 2000 <= yr <= 2030 and 1 <= mo <= 12:
            return yr, mo

    # e.g. mar08 at end of filename
    m = re.search(r'([a-z]{3})(\d{2})$', s)
    if m:
        mo = MONTH_MAP.get(m.group(1))
        yr = 2000 + int(m.group(2))
        if mo and 2000 <= yr <= 2030:
            return yr, mo

    return None, None


def extract_drug_name(fname):
    """Extract the drug name prefix from a PSD filename."""
    s = fname.lower().replace('%20', '-').replace(' ', '-').replace('.pdf', '')
    s = re.sub(r'\.docx', '', s)
    m = re.match(r'^(.+?)-psd', s)
    if m:
        return m.group(1).strip('-')
    return s


def classify_therapy(drug):
    d = drug.lower()
    for area, keywords in THERAPY_KEYWORDS.items():
        for kw in keywords:
            if kw in d:
                return area
    return 'Other'


def main():
    records = []
    skipped = []

    for fname in sorted(os.listdir(SCRIPT_DIR)):
        if not fname.endswith('.pdf'):
            continue
        if 'psd' not in fname.lower():
            skipped.append(fname)
            continue

        year, month = parse_date_from_filename(fname)
        if year is None:
            skipped.append(fname)
            continue

        drug = extract_drug_name(fname)
        therapy = classify_therapy(drug)

        records.append({
            'filename': fname,
            'drug': drug,
            'year': year,
            'month': month,
            'therapy': therapy,
        })

    print(f"Parsed {len(records)} PSD records. Skipped {len(skipped)} files.")

    # Aggregate stats
    by_year = defaultdict(int)
    by_therapy = defaultdict(int)
    by_drug = defaultdict(int)

    for r in records:
        by_year[r['year']] += 1
        by_therapy[r['therapy']] += 1
        by_drug[r['drug']] += 1

    top_drugs = sorted(by_drug.items(), key=lambda x: -x[1])

    # Multi-submission drugs (4+ appearances)
    multi_submission = [
        {'drug': d, 'count': c}
        for d, c in top_drugs if c >= 2
    ][:40]

    all_years = sorted(by_year.keys())

    output = {
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'isEstimate': False,
        'total': len(records),
        'uniqueDrugs': len(by_drug),
        'yearRange': [min(all_years), max(all_years)] if all_years else [None, None],
        'byYear': {str(y): by_year[y] for y in all_years},
        'byTherapy': dict(sorted(by_therapy.items(), key=lambda x: -x[1])),
        'topDrugs': multi_submission,
    }

    out_path = os.path.join(SCRIPT_DIR, 'psd_data.js')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('// Auto-generated by generate_psd_data.py — do not edit manually\n')
        f.write('const PSD_DATA = ')
        json.dump(output, f, indent=2)
        f.write(';\n')

    print(f"\nSaved: {out_path}")
    print(f"  Total PSDs:    {output['total']}")
    print(f"  Unique drugs:  {output['uniqueDrugs']}")
    print(f"  Year range:    {output['yearRange'][0]} – {output['yearRange'][1]}")
    print(f"\nNow open psd_dashboard.html in your browser!")

if __name__ == '__main__':
    main()
