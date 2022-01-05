import argparse
import collections
from datetime import datetime
import gzip
import hashlib
import json
import math
import os
import pandas as pd
import pkgutil
import pwd
import requests
import tqdm

from str_analysis.utils.canonical_repeat_unit import compute_canonical_motif

# Map STR locus ids to readable names for STR loci that are adjacent to the main known pathogenic loci
from str_analysis.utils.export_json import export_json
from str_analysis.utils.known_pathogenic_strs_tsv import parse_known_pathogenic_strs_tsv

ADJACENT_REPEAT_LABELS = {
    "ATXN7_GCC": "Adjacent Right STR",
    "ATXN8OS_CTA": "Adjacent Left STR",
    "HTT_CCG": "Adjacent Right STR",
    "FXN_A": "Adjacent Left Homopolymer",
    "CNBP_CAGA": "Adjacent Right STR #1",
    "CNBP_CA": "Adjacent Right STR #2",
    "NOP56_CGCCTG": "Adjacent Right STR",
}

# Map gene name to Ensembl gene id for genes that contain known pathogenic STRs
GENE_NAME_TO_GENE_ID = {
    'ATXN8': 'ENSG00000230223',
    'AFF2': 'ENSG00000155966',
    'AR': 'ENSG00000169083',
    'ARX': 'ENSG00000004848',
    'ATN1': 'ENSG00000111676',
    'ATXN1': 'ENSG00000124788',
    'ATXN10': 'ENSG00000130638',
    'ATXN2': 'ENSG00000204842',
    'ATXN3': 'ENSG00000066427',
    'ATXN7': 'ENSG00000163635',
    'BEAN1': 'ENSG00000166546',
    'C9orf72': 'ENSG00000147894',
    'CACNA1A': 'ENSG00000141837',
    'CBL2': 'ENSG0000011039',
    'CNBP': 'ENSG00000169714',
    'COMP': 'ENSG00000105664',
    'CSTB': 'ENSG00000160213',
    'DAB1': 'ENSG00000173406',
    'DIP2B': 'ENSG00000066084',
    'DMD': 'ENSG00000198947',
    'DMPK': 'ENSG00000104936',
    'EIF4A3': 'ENSG00000141543',
    'FMR1': 'ENSG00000102081',
    'FOXL2': 'ENSG00000183770',
    'FXN': 'ENSG00000165060',
    'GIPC1': 'ENSG00000123159',
    'GLS': 'ENSG00000115419',
    'HOXA13': 'ENSG00000106031',
    'HOXD13': 'ENSG00000128714',
    'HTT': 'ENSG00000197386',
    'JPH3': 'ENSG00000154118',
    'LOC642361': 'ENSG00000272447',
    'LRP12': 'ENSG00000147650',
    'MARCHF6': 'ENSG00000145495',
    'NIPA1': 'ENSG00000170113',
    'NOP56': 'ENSG00000101361',
    'NOTCH2NLC': 'ENSG00000286219',
    'PABPN1': 'ENSG00000100836',
    'PHOX2B': 'ENSG00000109132',
    'PPP2R2B': 'ENSG00000156475',
    'PRDM12': 'ENSG00000130711',
    'PRNP': 'ENSG00000171867',
    'RAPGEF2': 'ENSG00000109756',
    'RFC1': 'ENSG00000035928',
    'RUNX2': 'ENSG00000124813',
    'SAMD12': 'ENSG00000177570',
    'SOX3': 'ENSG00000134595',
    'STARD7': 'ENSG00000084090',
    'TBP': 'ENSG00000112592',
    'TBX1': 'ENSG00000184058',
    'TCF4': 'ENSG00000196628',
    'TNRC6A': 'ENSG00000090905',
    'VWA1': 'ENSG00000179403',
    'XYLT1': 'ENSG00000103489',
    'YEATS2': 'ENSG00000163872',
    'ZIC2': 'ENSG00000043355',
    'ZIC3': 'ENSG00000156925',
}

# Round ages to the nearest N years so that they can be shared publicly without increasing identifiability
AGE_RANGE_SIZE = 5

# Truncate the age distribution at this lower and upper bound.
LOWER_AGE_CUTOFF = 20
UPPER_AGE_CUTOFF = 80

# Show age for not more than this many of the most expanded samples per locus for each sex/population bucket
MAX_AGES_PER_BUCKET_TO_DISPLAY_IN_THE_READVIZ_SECTION = 100

# Use this value instead of the age range for samples where age is not available or not shown.
AGE_NOT_AVAILABLE = "age_not_available"

PCR_INFO_NOT_AVAILABLE = "pcr_info_not_available"


# Show age only for the these larger sub-populations to avoid increasing identifiability in smaller populations
POPULATIONS_WITH_AGE_DISPLAYED_IN_READVIZ_SECTION = {"sas", "oth", "asj", "amr", "fin", "eas", "afr", "nfe", "mid"}


# Fraction of genotypes that can be missing for a locus before generating an error
MISSING_GENOTYPES_ERROR_THRESHOLD = 0.01

# Fraction of readviz images that can be missing for a locus before generating an error
MISSING_READVIZ_ERROR_THRESHOLD = 0.01

# Expected number of known pathogenic repeats
EXPECTED_N_KNOWN_PATHOGENIC_REPEATS = 59

# Add this "salt" value to the sha512 hash to prevent dictionary attacks on the encrypted sample ids
salt = pwd.getpwuid(os.getuid()).pw_name


def parse_args():
    """Parse command-line args, perform basic validation, and then return the args object."""

    p = argparse.ArgumentParser()
    p.add_argument(
        "--expansion-hunter-tsv",
        default="~/code/str-analysis/local_files/gnomad_str_data/data/combined_expansion_hunter.19243_json_files.variants.tsv",
        help="Table generated by running python3 -m str_analysis.combine_expansionhunter_json_to_tsv on all samples "
             "called by ExpansionHunter."
    )
    p.add_argument(
        "--non-ref-motif-tsv",
        default="~/code/str-analysis/local_files/gnomad_str_data/data/combined.173160_json_files.tsv",
        help="Table generated by running python3 -m str_analysis.combine_json_to_tsv on all loci called by "
             "str_analysis.call_non_ref_pathogenic_motifs.",
    )
    p.add_argument(
        "--gnomad-metadata-tsv",
        default="~/code/sample_metadata/metadata/gnomad_v3.1_metadata_v3.1.tsv.gz",
        help="gnomAD metadata table path.",
    )
    p.add_argument(
        "--known-pathogenic-strs-tsv",
        default="~/code/str-analysis/local_files/gnomad_str_data/known_pathogenic_strs.tsv",
        help="Table of known pathogenic STRs.",
    )
    p.add_argument(
        "--existing-readviz-filename-list",
        help="A text file that lists all readviz .svg filenames that exist (one per line). These are the encrypted "
             "public filenames that don't contain sample ids - for example: ffa0880117e0791d51b0ef85b56f3a54216.svg",
    )
    p.add_argument(
        "--output-dir",
        default="gs://gnomad-browser/STRs",
        help="Where to write output files. Supports local and Google storage (gs://) paths.",
    )
    args = p.parse_args()

    for path in args.expansion_hunter_tsv, args.non_ref_motif_tsv, args.gnomad_metadata_tsv, \
                args.known_pathogenic_strs_tsv:
        if not os.path.isfile(os.path.expanduser(path)):
            p.error(f"{path} file not found")

    return args


def load_data_df(args):
    """Load the tables specified by args.expansion_hunter_tsv, args.non_ref_motif_tsv, and args.gnomad_metadata_tsv.
    Rename and select relevant columns, combine the tables, then return a single combined table.

    Args:
        args (argparse.Namespace): The argparse parsed arguments object.

    Return:
        pandas.DataFrame: The result of combining the 3 tables.
    """

    print(f"Loading {args.expansion_hunter_tsv}")

    def split_by_forward_slash(expansion_hunter_call_repeat_unit):
        repeat_units = expansion_hunter_call_repeat_unit.split("/")
        return repeat_units[0].strip(), repeat_units[-1].strip()

    def process_sample_id(sample_id):
        sample_id = sample_id.replace("RP-1400::", "").replace("v3.1::", "")
        return sample_id.strip().replace(" ", "_").replace("-", "_").split(".")[0].split("_SM_")[0]

    # Parse ExpansionHunter tsv
    df = pd.read_table(args.expansion_hunter_tsv)
    df.loc[:, "SampleId"] = df.SampleId.apply(process_sample_id)
    df.loc[:, "Motif: Allele 1"] = df["RepeatUnit"]
    df.loc[:, "Motif: Allele 2"] = df["RepeatUnit"]
    df.loc[:, "ReadvizFilename"] = df["SampleId"] + "." + df["LocusId"] + ".svg"
    df = df[[
        "SampleId", "LocusId", "VariantCatalog_Gene", "VariantId", "ReferenceRegion",
        "Motif: Allele 1", "Motif: Allele 2",
        "Num Repeats: Allele 1", "Num Repeats: Allele 2",
        "Genotype", "GenotypeConfidenceInterval",
        "RepeatUnit", "ReadvizFilename",
    ]]

    # Parse the args.non_ref_motif_tsv generated by call_non_ref_pathogenic_motifs
    print(f"Loading {args.non_ref_motif_tsv}")
    non_ref_motifs_df = pd.read_table(args.non_ref_motif_tsv)
    non_ref_motifs_df = non_ref_motifs_df[~non_ref_motifs_df["expansion_hunter_call_genotype"].isna()]

    non_ref_motifs_df["Motif: Allele 1"], non_ref_motifs_df["Motif: Allele 2"] = zip(
        *non_ref_motifs_df["expansion_hunter_call_repeat_unit"].apply(split_by_forward_slash))

    non_ref_motifs_df.loc[:, "Num Repeats: Allele 1"], non_ref_motifs_df.loc[:, "Num Repeats: Allele 2"] = zip(
        *non_ref_motifs_df["expansion_hunter_call_genotype"].apply(split_by_forward_slash))

    non_ref_motifs_df.loc[:, "SampleId"] = non_ref_motifs_df.sample_id.apply(process_sample_id)
    non_ref_motifs_df.loc[:, "LocusId"] = non_ref_motifs_df["locus_id"]
    non_ref_motifs_df.loc[:, "VariantCatalog_Gene"] = non_ref_motifs_df["locus_id"]
    non_ref_motifs_df.loc[:, "VariantId"] = non_ref_motifs_df["locus_id"]
    non_ref_motifs_df.loc[:, "ReferenceRegion"] = non_ref_motifs_df["locus_coords"]
    non_ref_motifs_df.loc[:, "Genotype"] = non_ref_motifs_df["expansion_hunter_call_genotype"]
    non_ref_motifs_df.loc[:, "GenotypeConfidenceInterval"] = non_ref_motifs_df["expansion_hunter_call_CI"]
    non_ref_motifs_df.loc[:, "RepeatUnit"] = None   # will be set later
    non_ref_motifs_df.loc[:, "ReadvizFilename"] = non_ref_motifs_df["expansion_hunter_call_reviewer_svg"]
    non_ref_motifs_df = non_ref_motifs_df[[
        "SampleId", "LocusId", "VariantCatalog_Gene", "VariantId", "ReferenceRegion",
        "Motif: Allele 1", "Motif: Allele 2",
        "Num Repeats: Allele 1", "Num Repeats: Allele 2",
        "Genotype", "GenotypeConfidenceInterval",
        "RepeatUnit", "ReadvizFilename",
    ]]

    df = df[~df["LocusId"].isin(set(non_ref_motifs_df["LocusId"]))]
    df = pd.concat([df, non_ref_motifs_df])

    # Parse gnomAD metadata tsv
    print(f"Loading {args.gnomad_metadata_tsv}")
    gnomad_df = pd.read_table(args.gnomad_metadata_tsv)
    gnomad_df = gnomad_df[gnomad_df.release]
    gnomad_df.loc[:, "age"] = gnomad_df["project_meta.age"].fillna(gnomad_df["project_meta.age_alt"])
    gnomad_df["age"].fillna(AGE_NOT_AVAILABLE, inplace=True)
    gnomad_df.loc[:, "pcr_protocol"] = gnomad_df["project_meta.product"].apply(
        lambda s: pd.NA if not s or pd.isna(s) else (True if "pcr-free" in s.lower() else False), convert_dtype="boolean")
    gnomad_df["pcr_protocol"].fillna(gnomad_df["project_meta.v2_pcr_free"].astype("boolean"), inplace=True)
    gnomad_df["pcr_protocol"].fillna(PCR_INFO_NOT_AVAILABLE, inplace=True)
    gnomad_df.loc[:, "pcr_protocol"] = gnomad_df["pcr_protocol"].replace({True: "pcr_free", False: "pcr_plus"})

    gnomad_df = gnomad_df[[
        "s", "population_inference.pop", "sex_imputation.sex_karyotype",
        "age", "pcr_protocol",
    ]]
    gnomad_df.loc[:, "s"] = gnomad_df.s.apply(process_sample_id)

    unknown_sample_ids = set(df.SampleId) - set(gnomad_df.s)
    if len(unknown_sample_ids) > 0:
        print(f"WARNING: Dropping {len(unknown_sample_ids)} sample ids in {args.expansion_hunter_tsv} that "
              f"were not found in the gnomAD metadata table, or were found but are not 'release': ", unknown_sample_ids)

    # Merge the data frames
    print(f"Combining STR data tables with gnomAD metadata")
    df = pd.merge(left=df, right=gnomad_df, how="inner", left_on="SampleId", right_on="s").drop(columns="s")

    print(f"Found {len(set(df.SampleId))} gnomAD 'release' samples")
    for locus_id in sorted(set(df.LocusId)):
        print(f"Found {len(set(df[df.LocusId == locus_id].SampleId))} {locus_id} 'release' samples")

    return df


def init_gnomad_json(df):
    """Compute an initial .json structure with a key for each STR locus. Initialize sub-dictionaries that will hold
    the allele count histogram and scatter plot counts for each locus.

    Args:
        df (pandas.DataFrame): Combined DataFrame generated by load_data_df(..)

    Return:
        dict: An initial version of the main .json structure being generated by this script.
    """

    # Compute the STR loci
    df = df[["LocusId", "VariantCatalog_Gene", "VariantId", "ReferenceRegion", "RepeatUnit"]]
    df = df.drop_duplicates()

    # Init sub-dictionaries for each locus
    gnomad_json = {}
    for _, row in tqdm.tqdm(df.iterrows(), unit=" rows", total=len(df)):
        locus_id = row["LocusId"]
        variant_id = row["VariantId"]
        adjacent_repeat_label = ADJACENT_REPEAT_LABELS[variant_id] if variant_id in ADJACENT_REPEAT_LABELS else None

        gene_name = row["VariantCatalog_Gene"]
        if locus_id not in gnomad_json:
            gnomad_json[locus_id] = {
                "LocusId": locus_id,
                "GeneName": gene_name,
            }

        repeat_specific_fields = {
            "ReferenceRegion": row["ReferenceRegion"],
            "ReferenceRepeatUnit": row.get("RepeatUnit"),
            "AlleleCountHistogram":  {},
            "AlleleCountScatterPlot": {},
            "AgeDistribution": {},
        }

        if adjacent_repeat_label is not None:
            if "AdjacentRepeats" not in gnomad_json[locus_id]:
                gnomad_json[locus_id]["AdjacentRepeats"] = {}

            if variant_id not in gnomad_json[locus_id]["AdjacentRepeats"]:
                gnomad_json[locus_id]["AdjacentRepeats"][adjacent_repeat_label] = repeat_specific_fields
        else:
            gnomad_json[locus_id].update(repeat_specific_fields)

    return gnomad_json


def add_gene_ids(gnomad_json):
    """Add the GeneId field to gnomad_json.

    Args:
        gnomad_json (dict): The main .json structure being generated by this script.
    """
    for locus_id in gnomad_json:
        gene_name = gnomad_json[locus_id]["GeneName"]
        if gene_name in GENE_NAME_TO_GENE_ID:
            gnomad_json[locus_id]["GeneId"] = GENE_NAME_TO_GENE_ID[gene_name]
            continue

        # Get gene id via the Ensembl API.
        response = None
        while response is None or not response.ok or not response.json():
            print(f"Getting gene id for {gene_name}")
            request_url = f"https://rest.ensembl.org/lookup/symbol/homo_sapiens/{gene_name}"
            request_url += "?content-type=application/json;expand=1"
            response = requests.get(request_url)

        response_json = response.json()
        if not response_json.get('id'):
            print("Unable to get ensembl details for", gene_name)
            continue

        gene_id = response_json['id']
        gnomad_json[locus_id]["GeneId"] = gene_id


def add_known_pathogenic_STR_annotations(args, gnomad_json):
    """Load the args.known_pathogenic_strs_tsv table and add metadata from it to gnomad_json.

    Args:
        args (argparse.Namespace): The argparse parsed arguments object.
        gnomad_json (dict): The main .json structure being generated by this script.
    """

    known_pathogenic_strs_info = parse_known_pathogenic_strs_tsv(args.known_pathogenic_strs_tsv)
    if len(known_pathogenic_strs_info) != EXPECTED_N_KNOWN_PATHOGENIC_REPEATS:
        raise ValueError(f"{args.known_pathogenic_strs_tsv} contains {len(known_pathogenic_strs_info)} pathogenic loci."
                         f" Expected {EXPECTED_N_KNOWN_PATHOGENIC_REPEATS} loci.")
    locus_ids_without_annotations = set(gnomad_json.keys()) - set(known_pathogenic_strs_info)
    if locus_ids_without_annotations:
        raise ValueError(f"LocusIds not found in known pathogenic STRs spreadsheet: {locus_ids_without_annotations}")

    # Compute STRipy urls
    for locus_id in gnomad_json:
        stripy_name = locus_id
        stripy_url = f"https://stripy.org/database/{stripy_name}"
        r = requests.get(stripy_url)
        if r.ok and "invalid locus" not in r.content.decode("UTF-8").lower():
            known_pathogenic_strs_info[locus_id]["STRipyName"] = stripy_name
        else:
            print(f"WARNING: STRipy page not found for {locus_id}")

    # Add the metadata to gnomad_json
    for locus_id in gnomad_json:
        gnomad_json[locus_id].update(known_pathogenic_strs_info[locus_id])


def compute_most_common_motif_lookup_dict(df):
    """Create a lookup dictionary that maps (LocusId, canonical motif) pairs to the most common non-canonical motif
    among observed motifs that share this same canonical motif. This allows converting motif rearrangements such as
    AAAAG, AAAGA, AAGAA, etc. at the RFC1 locus into "AAAAG" which is the rearrangement that is seen most frequently
    in the general population. Similarly, for the HTT locus, "AGC", "CAG", and "GCA" would get converted to "CAG" since
    that's the only rearrangement that's seen in practice.

    Args:
         df (pandas.DataFrame): Combined DataFrame generated by load_data_df(..)

    Return:
         dict: A dictionary of the form {("RFC1", "AAAAG"): "AAAAG", ...}
    """

    # First, create a dictionary that maps each (LocusId, Motif) pair to the number of times it occurs in df.
    # Example entries:  ('RFC1', 'GAAAG'): 805,  ('RFC1', 'AAAGG'): 774, etc.
    motif_counts = pd.concat([
        df[["LocusId", "Motif: Allele 1"]].rename(columns={"Motif: Allele 1": "Motif"}),
        df[["LocusId", "Motif: Allele 2"]].rename(columns={"Motif: Allele 2": "Motif"}),
    ]).value_counts().to_dict()

    # Create a new dictionary that maps (LocusId, canonical motif) pairs to the most common non-canonical motif
    # observed among motifs that share the same canonical motif. Using the example from the previous comment, it would
    # map ('RFC1', 'AAAGG') to 'GAAAG' rather than 'AAAGG' because 'GAAAG' is observed 805 times while 'AAAGG' is only
    # observed 774 times in df.
    most_common_motif_lookup = {}
    for (locus_id, motif), counter in motif_counts.items():
        key = (locus_id, compute_canonical_motif(motif))
        if key not in most_common_motif_lookup:
            most_common_motif_lookup[key] = (motif, counter)
            continue

        previous_motif, previous_counter = most_common_motif_lookup[key]
        if previous_counter < counter:
            most_common_motif_lookup[key] = (motif, counter)

    # Drop the counter from the value
    most_common_motif_lookup = {key: motif for key, (motif, _) in most_common_motif_lookup.items()}

    return most_common_motif_lookup


def add_motif_classification_field(gnomad_json, most_common_motif_lookup):
    """For repeats where the pathogenic motif differs from the reference motif, add info on which motifs are known to be
    disease-associated and which are benign.

    Args:
        gnomad_json (dict): The main .json structure being generated by this script.
        most_common_motif_lookup (dict): The dictionary generated by compute_most_common_motif_lookup_dict(..)
    """

    non_ref_pathogenic_motif_info = json.loads(pkgutil.get_data("str_analysis", "data/locus_info.json"))

    for locus_id in gnomad_json:
        gene_name = gnomad_json[locus_id]["GeneName"]
        if gene_name not in non_ref_pathogenic_motif_info:
            continue

        gnomad_json[locus_id]["RepeatUnitClassification"] = {}
        for classification, motifs in non_ref_pathogenic_motif_info[gene_name]["Motifs"].items():
            for motif in motifs:
                canonical_motif = compute_canonical_motif(motif)
                motif_key = most_common_motif_lookup.get((locus_id, canonical_motif))
                if motif_key is None:
                    # If this known-benign or known-pathogenic motif wasn't detected in any gnomAD samples, just
                    # include it as-is, the way it's recorded in data/locus_info.json
                    motif_key = motif
                gnomad_json[locus_id]["RepeatUnitClassification"][motif_key] = classification


def add_histograms_and_compute_readviz_paths(df, gnomad_json, most_common_motif_lookup):
    """Populate the AlleleCountHistogram, AlleleCountScatterPlot and AgeDistribution. Also, compute encrypted readviz
    paths and add these & other metadata to readviz_json.

    Args:
        df (pandas.DataFrame): Combined DataFrame generated by load_data_df(..)
        gnomad_json (dict): The main .json structure being generated by this script.
        most_common_motif_lookup (dict): The dictionary generated by compute_most_common_motif_lookup_dict(..)

    Return:
        (list, dict): 2-tuple containing (readviz_paths_to_rename, readviz_json) where
            readviz_paths_to_rename is a list of 2-tuples that matches the original readviz svg filename with the
                corresponding encrypted filename that can be made public.
            readviz_json is the .json data structure that will be loaded into the gnomAD browser to generate the
                readviz section of the STR pages. It contains the encrypted readviz filename and associated metadata
                for each sample.
    """

    readviz_paths_to_rename = set()
    readviz_json = {}
    age_counter = collections.defaultdict(int)

    df = df.sort_values(["Num Repeats: Allele 2", "Num Repeats: Allele 1", "Motif: Allele 2", "Motif: Allele 1"], ascending=False)
    for _, row in tqdm.tqdm(df.iterrows(), unit=" rows", total=len(df)):
        locus_id = row["LocusId"]
        variant_id = row["VariantId"]

        is_adjacent_repeat = variant_id in ADJACENT_REPEAT_LABELS
        adjacent_repeat_label = ADJACENT_REPEAT_LABELS[variant_id] if is_adjacent_repeat else None
        canonical_motif1 = compute_canonical_motif(row["Motif: Allele 1"])
        canonical_motif2 = compute_canonical_motif(row["Motif: Allele 2"])
        motif1 = most_common_motif_lookup[locus_id, canonical_motif1]
        motif2 = most_common_motif_lookup[locus_id, canonical_motif2]

        # Get gnomAD fields
        sex_karyotype = row["sex_imputation.sex_karyotype"]
        population = row["population_inference.pop"]
        pcr_protocol = row["pcr_protocol"]

        # Compute age_range
        if row["age"] == AGE_NOT_AVAILABLE:
            age_range = AGE_NOT_AVAILABLE
        else:
            age = int(row["age"])
            age_lower_bound = AGE_RANGE_SIZE * math.floor(age/AGE_RANGE_SIZE)
            age_upper_bound = AGE_RANGE_SIZE * math.ceil((age + 0.1)/AGE_RANGE_SIZE)
            assert age_lower_bound != age_upper_bound
            if age_upper_bound <= LOWER_AGE_CUTOFF:
                age_range = f"<{LOWER_AGE_CUTOFF}"
            elif age_lower_bound >= UPPER_AGE_CUTOFF:
                age_range = f">{UPPER_AGE_CUTOFF}"
            else:
                age_range = f"{age_lower_bound}-{age_upper_bound}"

        age_range_to_show_in_readviz_section = AGE_NOT_AVAILABLE
        if (population in POPULATIONS_WITH_AGE_DISPLAYED_IN_READVIZ_SECTION
                and age_counter[locus_id, sex_karyotype] < MAX_AGES_PER_BUCKET_TO_DISPLAY_IN_THE_READVIZ_SECTION):
            age_counter[locus_id, sex_karyotype] += 1
            age_range_to_show_in_readviz_section = age_range

        # Get num_repeats1, num_repeats2
        try:
            num_repeats1 = int(float(row["Num Repeats: Allele 1"]))
            num_repeats2 = float(row["Num Repeats: Allele 2"])
        except ValueError as e:
            print("Num Repeats parse error", e, row["Genotype"], row["GenotypeConfidenceInterval"], ". Skipping..")
            continue

        if sex_karyotype == "XY" and "X" in row["ReferenceRegion"]:
            is_hemizygous = True
            if math.isnan(num_repeats2) or num_repeats2 == num_repeats1:
                num_repeats2 = num_repeats1
            else:
                print(f"ERROR: Locus is male and on chrX, but has different values for allele1, allele2: {row.to_dict()}")
                continue
        else:
            is_hemizygous = False

        num_repeats2 = int(num_repeats2)

        # Update histogram and scatter plot counts
        histogram_key1 = f"{population}/{sex_karyotype}/{motif1}"
        histogram_key2 = f"{population}/{sex_karyotype}/{motif2}"
        scatter_plot_key = f"{population}/{sex_karyotype}/{motif1}/{motif2}"
        age_distribution_key = f"{age_range}"

        if is_adjacent_repeat:
            data_dict = gnomad_json[locus_id]["AdjacentRepeats"][adjacent_repeat_label]
        else:
            data_dict = gnomad_json[locus_id]

        for histogram_key in histogram_key1, histogram_key2:
            if histogram_key not in data_dict["AlleleCountHistogram"]:
                data_dict["AlleleCountHistogram"][histogram_key] = collections.defaultdict(int)

        data_dict["AlleleCountHistogram"][histogram_key1][f"{num_repeats1}"] += 1
        if not is_hemizygous:
            data_dict["AlleleCountHistogram"][histogram_key2][f"{num_repeats2}"] += 1

        if scatter_plot_key not in data_dict["AlleleCountScatterPlot"]:
            data_dict["AlleleCountScatterPlot"][scatter_plot_key] = collections.defaultdict(int)
        data_dict["AlleleCountScatterPlot"][scatter_plot_key][f"{num_repeats1}/{num_repeats2}"] += 1

        if age_range != AGE_NOT_AVAILABLE:
            if age_distribution_key not in data_dict["AgeDistribution"]:
                data_dict["AgeDistribution"][age_distribution_key] = collections.defaultdict(int)
            for num_repeats in num_repeats1, num_repeats2:
                data_dict["AgeDistribution"][age_distribution_key][f"{num_repeats}"] += 1

        # Update readviz metadata
        if not is_adjacent_repeat:
            encrypted_svg_prefix = hashlib.sha512(f"{locus_id}_{row['SampleId']}_{salt}".encode("UTF-8")).hexdigest()
            # The sha digest is 128 letters long - which is too long for a filename. Use only the first 35 letters.
            encrypted_svg_filename = f"{encrypted_svg_prefix[:35]}.svg"

            original_svg_filename = row["ReadvizFilename"]
            readviz_paths_to_rename.add((original_svg_filename, f"{locus_id}/{encrypted_svg_filename}"))

            if locus_id not in readviz_json:
                readviz_json[locus_id] = []

            readviz_json[locus_id].append({
                "Allele1Motif": motif1,
                "Allele2Motif": motif2,
                "Allele1HistogramKey": histogram_key1,
                "Allele2HistogramKey": histogram_key2 if not is_hemizygous else None,
                "ScatterPlotKey": scatter_plot_key,
                "ScatterPlotX": num_repeats2,
                "ScatterPlotY": num_repeats1,
                "Sex": sex_karyotype,
                "Age": age_range_to_show_in_readviz_section,
                "Population": population,
                "PcrProtocol": pcr_protocol,
                "Genotype": row["Genotype"],
                "GenotypeConfidenceInterval": row["GenotypeConfidenceInterval"],
                "ReadvizFilename": encrypted_svg_filename,
            })

    return list(readviz_paths_to_rename), readviz_json


def sort_keys(gnomad_json):
    """Sort keys in the output json. Python built-in dictionaries preserve key order since python3.6, so this works.
    For example, for the "AFF2" locus this sorts keys so that

    {
      "ReferenceRegion": "chrX:148500631-148500691",
      "ReferenceRepeatUnit": "GCC",
      "LocusId": "AFF2",
      "AgeDistribution": {
         "35-40": {
            "4": 3,
            "0": 10,
            "2": 2,
            ...
         },
         "20-25": {
            "5": 5,
            "6": 12,
            "0": 10,
            "2": 2,
            "4": 3,
            ...
        },
      }
    }

    is converted to

    {
      "LocusId": "AFF2",
      "ReferenceRegion": "chrX:148500631-148500691",
      "ReferenceRepeatUnit": "GCC",
      "AgeDistribution": {
         "20-25": {
            "0": 10,
            "2": 2,
            "4": 3,
            "5": 5,
            "6": 12,
            ...
        },
         "35-40": {
            "0": 10,
            "2": 2,
            "4": 3,
            ...
         },
      }
    }

    Args:
        gnomad_json (dict): The main .json structure being generated by this script.
    """

    def sort_by_key(key_type=str):
        def key_lookup(key_value):
            return key_type(key_value[0])
        return key_lookup

    def top_level_sort_order(key_value):
        # Sort top-level keys so that the histograms are last
        return key_value[0] in ("AlleleCountHistogram", "AlleleCountScatterPlot", "AgeDistribution"), key_value[0]

    for locus_id, locus_data in gnomad_json.items():
        for histogram_name, histogram_key_type in (
            ("AlleleCountHistogram", int),
            ("AlleleCountScatterPlot", str),
            ("AgeDistribution", int),
        ):
            # `histogram_key` here refers to the top level keys in the histogram
            # For example the "20-25" age range is a `histogram_key` within "AgeDistribution"
            # Each of the age ranges within "AgeDistribution" has a nested dict. 
            # e.g., "0" is a key within "20-25": 
            # gnomad_json["AFF2"]["AgeDistribution"]["20-25"]["0"] = 10
            # This first `for` loop below sorts these nested dicts (e.g., sorts the dicts within the age range "20-25")
            for histogram_key in locus_data[histogram_name]:
                locus_data[histogram_name][histogram_key] = {
                    key: value for key, value in sorted(
                        locus_data[histogram_name][histogram_key].items(), key=sort_by_key(key_type=histogram_key_type))
                }
            # This sorts the `histogram_key` values
            # e.g, this sorts "20-25", "25-30", "30-35", etc. within "AgeDistribution" 
            locus_data[histogram_name] = {
                key: value for key, value in sorted(
                    locus_data[histogram_name].items(), key=sort_by_key())
            }

        # This sorts the top level keys, which contain the histogram names above (e.g., AgeDistribution)
        # and other keys, like "Diseases", "GeneID", "GeneName", etc.
        gnomad_json[locus_id] = {
            key: value for key, value in sorted(gnomad_json[locus_id].items(), key=top_level_sort_order)
        }


def remove_readviz_filenames_that_dont_exist(args, readviz_json):
    """Remove ReadvizFilename entries that aren't listed in args.existing_readviz_filename_list.

    Args:
        args (argparse.Namespace): The argparse parsed arguments object.
        readviz_json (dict): The .json data structure that will be loaded into the gnomAD browser to generate the
                readviz section of the STR pages. It contains the encrypted readviz filename and associated
                metadata for each sample.
    """
    readviz_filenames_df = pd.read_table(args.existing_readviz_filename_list, names=["filenames"])
    readviz_filenames_list = readviz_filenames_df["filenames"]
    readviz_filenames_set = set(readviz_filenames_list)

    if len(readviz_filenames_list) > len(readviz_filenames_set):
        raise ValueError(f"{args.existing_readviz_filename_list} contains duplicate entries")

    for locus_id in readviz_json:
        removed = total = 0
        for record in readviz_json[locus_id]:
            total += 1
            if record["ReadvizFilename"] not in readviz_filenames_set:
                record["ReadvizFilename"] = None
                removed += 1

        message = (f"{locus_id:20s}:  {removed} out of {total} ({100*removed/total:0.2f}%) readviz images removed "
            f"because they are missing from {args.existing_readviz_filename_list}")

        if removed/total > MISSING_READVIZ_ERROR_THRESHOLD:
            raise ValueError(message)

        print(message)


def validate_json(df, gnomad_json, readviz_json):
    """Perform basic checks to validate the gnomad_json and readviz_json data structure.

    Args:
        df (pandas.DataFrame): Combined DataFrame generated by load_data_df(..).
        gnomad_json (dict): The main .json structure being generated by this script.
        readviz_json (dict): The .json data structure that will be loaded into the gnomAD browser to generate the
                readviz section of the STR pages. It contains the encrypted readviz filename and associated
                metadata for each sample.
    """

    total_samples = len(set(df["SampleId"]))
    if len(gnomad_json) != EXPECTED_N_KNOWN_PATHOGENIC_REPEATS:
        raise ValueError(f"gnomad_json contains {len(gnomad_json)} pathogenic loci. "
            f"Expected {EXPECTED_N_KNOWN_PATHOGENIC_REPEATS} loci.")

    gnomad_json_str_loci = set(gnomad_json)
    readviz_json_str_loci = set(readviz_json)
    if gnomad_json_str_loci != readviz_json_str_loci:
        raise ValueError(f"gnomad_json locus ids are different from readviz_json locus ids:\n"
                         f"{gnomad_json_str_loci} \n{readviz_json_str_loci}")

    fraction_male_samples = sum(df["sex_imputation.sex_karyotype"] == "XY")/len(df)
    for locus_id, data in gnomad_json.items():
        # Check that expected keys are present and have non-null values.
        for key in "ReferenceRepeatUnit", "LocusId", "GeneName", "GeneId", "ReferenceRegion", \
                   "AlleleCountHistogram", "AlleleCountScatterPlot", "AgeDistribution", "Diseases":
            if data[key] is None:
                raise ValueError(f"{locus_id} {key} is None")

        # Check that expected keys are present in the data["Diseases"] dictionary and have non-null values.
        for key in "Symbol", "Name", "Inheritance", "PathogenicMin", "OMIM":
            for i, disease_data in enumerate(data["Diseases"]):
                if disease_data[key] is None:
                    raise ValueError(f"{locus_id} disease #{i} {key} is None")

        # Check that total counts in the histogram and scatter plot roughly match expectation, taking into account
        # hemizygous genotypes (which only contribute 1 count) and missing genotypes due to low coverage in some samples
        if "X" in data["ReferenceRegion"]:
            expected_counts_in_histogram = total_samples * (2 - fraction_male_samples)
        else:
            expected_counts_in_histogram = total_samples * 2

        for key, expected_counts in [
            ("AlleleCountHistogram", expected_counts_in_histogram),
            ("AlleleCountScatterPlot", total_samples)
        ]:
            total_counts_in_plot = sum([sum(d.values()) for d in data[key].values()])
            if not ((1 - MISSING_GENOTYPES_ERROR_THRESHOLD) * expected_counts < total_counts_in_plot <= expected_counts):
                raise ValueError(f"ERROR: {locus_id} total counts in {key} = {total_counts_in_plot} while expected counts = {expected_counts}")

        total_readviz_samples = len(readviz_json[locus_id])
        if total_readviz_samples < (1 - MISSING_GENOTYPES_ERROR_THRESHOLD) * total_samples:
            raise ValueError(f"{locus_id}: only {total_readviz_samples} readviz records. Expected {total_samples}")
        if total_readviz_samples > total_samples:
            raise ValueError(f"{locus_id}: found {total_readviz_samples} readviz records which is more than the total "
                             f"number of samples ({total_samples})")

        total_readviz_samples_with_image = sum(1 for r in readviz_json[locus_id] if r["ReadvizFilename"] is not None)
        if total_readviz_samples_with_image < (1 - MISSING_READVIZ_ERROR_THRESHOLD) * total_readviz_samples:
            raise ValueError(f"{locus_id}: found {total_readviz_samples_with_image} readviz images. Expected at "
                             f"least {total_readviz_samples}.")


def export_readviz_rename_list(readviz_paths_to_rename, readviz_rename_list_output_path):
    """Utility function for writing out the readviz_paths_to_rename data structure.

    Args:
        readviz_paths_to_rename (list): list of 2-tuples that matches the original readviz svg filename with the
            corresponding encrypted filename that can be made public.
        readviz_rename_list_output_path (str): Local output path where to write the readviz_paths_to_rename table.
    """

    if not readviz_rename_list_output_path.endswith(".gz"):
        raise ValueError(f"{readviz_rename_list_output_path} needs to end in .gz")

    print(f"Writing {readviz_rename_list_output_path}")
    with gzip.open(readviz_rename_list_output_path, "wt") as f:
        for a, b in readviz_paths_to_rename:
            f.write(f"{a}\t{b}\n")


def main():
    """Generate 3 files: the main gnomAD STR json data file which will be loaded into the gnomAD browser to populate
    the STR pages, a readviz_rename_list table which maps REViewer image filenames to the corresponding encrypted
    filenames which can be made public without revealing sample ids, and a readviz_paths_json file which contains
    metadata on samples and readviz images.
    """

    args = parse_args()

    # Generate the 3 data structures
    df = load_data_df(args)
    gnomad_json = init_gnomad_json(df)
    add_gene_ids(gnomad_json)
    add_known_pathogenic_STR_annotations(args, gnomad_json)
    most_common_motif_lookup = compute_most_common_motif_lookup_dict(df)
    add_motif_classification_field(gnomad_json, most_common_motif_lookup)
    readviz_paths_to_rename, readviz_json = add_histograms_and_compute_readviz_paths(df, gnomad_json, most_common_motif_lookup)
    if args.existing_readviz_filename_list:
        remove_readviz_filenames_that_dont_exist(args, readviz_json)
    sort_keys(gnomad_json)

    # Perform validity checks
    validate_json(df, gnomad_json, readviz_json)

    # Write out the data structures
    date_stamp = datetime.now().strftime("%Y_%m_%d")
    local_output_dir = os.path.expanduser(os.path.dirname(args.expansion_hunter_tsv))

    df.to_csv(f"{local_output_dir}/gnomAD_STR_calls_with_gnomAD_metadata_and_sample_ids__{date_stamp}.tsv.gz",
              compression="gzip", sep="\t", index=False, header=True)

    readviz_metadata_df = pd.DataFrame([
        {**readviz_record, **{"LocusId": locus_id}}
        for locus_id, readviz_records in readviz_json.items() for readviz_record in readviz_records
    ])
    readviz_metadata_df.to_csv(f"{local_output_dir}/gnomAD_STR_readviz_metadata__{date_stamp}.tsv.gz",
              compression="gzip", sep="\t", index=False, header=True)

    export_json(gnomad_json, f"{local_output_dir}/gnomAD_STR_distributions__{date_stamp}.json.gz", args.output_dir)
    export_json(readviz_json, f"{local_output_dir}/gnomAD_STR_readviz_metadata__{date_stamp}.json.gz", args.output_dir)
    export_readviz_rename_list(readviz_paths_to_rename, f"{local_output_dir}/readviz_rename_list__{date_stamp}.tsv.gz")

    print("Done")


if __name__ == "__main__":
    main()
    