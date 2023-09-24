#!/usr/bin/env python3

import argparse
import collections
import logging
import pathlib
import re

from intervaltree import Interval
import pandas as pd
import tqdm

from str_analysis.utils.misc_utils import parse_interval

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# The basic set of columns that need to be present in the input table
BASIC_INPUT_COLUMNS = [
    "SampleId", "LocusId", "VariantId", "Genotype", "GenotypeConfidenceInterval", "ReferenceRegion",
    "RepeatUnit", "Sex", "Num Repeats: Allele 2", "Coverage",
]

# Optional extra columns that will be added to the output if they're present in the input table
EXTRA_INPUT_COLUMNS = [
    "NumSpanningReads", "NumFlankingReads", "NumInrepeatReads",
    "NumAllelesSupportedBySpanningReads", "NumAllelesSupportedByFlankingReads", "NumAllelesSupportedByInrepeatReads",
]

def parse_args(args_list=None):
    """Parse command-line args and return the ArgumentParser args object.

    Args:
        arg_list (list): optional artificial list of command-line args to use for testing.

    Returns:
        args
    """

    p = argparse.ArgumentParser()
    p.add_argument(
        "-f",
        "--fam-file",
        help=".fam file that describes parent-child relationships of samples in the combined_str_calls_tsv. "
             "Only rows where both parent ids are specified will be used.",
        type=pathlib.Path,
        required=True,
    )
    p.add_argument(
        "--sample-id-column", 
        help="The column in the combined_str_calls_tsv that contains the sample id.",
        default="SampleId",
    )
    p.add_argument(
        "--output-locus-stats-tsv",
        action="store_true",
        help="Output a table of mendelian violation counts for each locus.",
    )
    p.add_argument(
        "--output-prefix",
        help="Output filename prefix.",
    )
    p.add_argument(
        "combined_str_calls_tsv",
        help=".tsv table that contains str calls for all individuals, created by the "
        "combine_expansion_hunter_json_results_to_tsv script",
        type=pathlib.Path,
    )
    args = p.parse_args(args=args_list)

    for path in [args.combined_str_calls_tsv, args.fam_file]:
        if not path.is_file():
            p.error(f"{path} not found")

    return args


def check_for_duplicate_keys(df, file_path):
    """Raises ValueError if it finds duplicate keys in the DataFrame.

    Args:
         df (pandas.DataFrame): Any pandas table
         file_path (str): File path of the table to include in the error message.

    Raises:
         ValueError
    """

    num_duplicate_keys = sum(df.index.duplicated())
    if num_duplicate_keys > 0:
        for idx, row in df[df.index.duplicated()].iterrows():
            logging.info("="*100)
            logging.info(f"ERROR: duplicate key: {idx}")
            logging.info(row)

        raise ValueError(f"Found {num_duplicate_keys} duplicate keys in {file_path}")


def parse_combined_str_calls_tsv_path(combined_str_calls_tsv_path, sample_id_column="SampleId"):
    """Parse a tsv table generated by combine_str_json_to_tsv.py, check for duplicates that have the same
    ("SampleId", "LocusId", "VariantId") and then return the table as a pandas DataFrame.

    Raises:
        ValueError: if it finds duplicates by ("SampleId", "LocusId", "VariantId")
    """

    combined_str_calls_df = pd.read_table(combined_str_calls_tsv_path, dtype=str)
    combined_str_calls_df_columns = set(combined_str_calls_df.columns)
    expected_columns = list(BASIC_INPUT_COLUMNS)
    if all(k in combined_str_calls_df_columns for k in EXTRA_INPUT_COLUMNS):
        expected_columns += list(EXTRA_INPUT_COLUMNS)

    if sample_id_column not in expected_columns:
        # replace "SampleId" with the user-specified sample id column name
        expected_columns = [sample_id_column] + [c for c in expected_columns if c != "SampleId"]


    combined_str_calls_df = combined_str_calls_df[expected_columns]
    #combined_str_calls_df = combined_str_calls_df.drop_duplicates()
    combined_str_calls_df.set_index([sample_id_column, "LocusId", "VariantId"], inplace=True)
    check_for_duplicate_keys(combined_str_calls_df, combined_str_calls_tsv_path)
    return combined_str_calls_df.reset_index()


def parse_fam_file(fam_file_path):
    """Reads the .fam file and returns a pandas DataFrame.

    Raises:
         ValueError: if the fam file contains duplicate individual ids.
    """

    fam_file_df = pd.read_table(
        fam_file_path,
        names=[
            "family_id",
            "individual_id",
            "father_id",
            "mother_id",
            "sex",
            "phenotype",
        ],
        dtype=str,
    )

    fam_file_df = fam_file_df[["individual_id", "father_id", "mother_id"]]
    fam_file_df = fam_file_df.drop_duplicates()
    fam_file_df.set_index("individual_id", inplace=True)
    fam_file_df = fam_file_df[~fam_file_df.father_id.isna() & ~fam_file_df.mother_id.isna()]
    fam_file_df = fam_file_df[(fam_file_df.father_id != "0") & (fam_file_df.mother_id != "0")]
    check_for_duplicate_keys(fam_file_df, fam_file_path)
    return fam_file_df.reset_index()


def group_rows_by_trio(combined_str_calls_df, sample_id_column="SampleId"):
    """Returns a list of 3-tuples containing the row of the proband, father and mother for full trios, as well as a
    2nd list of rows for samples that aren't part of a full trio.
    """
    print("Caching paternal & maternal genotypes")
    all_rows = {}
    for _, row in tqdm.tqdm(combined_str_calls_df.iterrows(), unit=" table rows", total=len(combined_str_calls_df)):
        if row["Genotype"] is not None:
            all_rows[(row[sample_id_column], row["LocusId"], row["VariantId"])] = row
            #all_rows[(row.Filename, row.LocusId, row.VariantId)] = row

    calls_counter = 0
    trio_ids = set()
    trio_rows = []
    other_rows = []
    print(f"{len(combined_str_calls_df):,d} total rows")
    combined_str_calls_df = combined_str_calls_df[~combined_str_calls_df.father_id.isna() & ~combined_str_calls_df.mother_id.isna()]
    print(f"{len(combined_str_calls_df):,d} rows remaining after filtering to rows that represent full trios")

    for _, row in tqdm.tqdm(combined_str_calls_df.iterrows(), unit=" table rows", total=len(combined_str_calls_df)):
        father_row = all_rows.get((row["father_id"], row["LocusId"], row["VariantId"]))
        mother_row = all_rows.get((row["mother_id"], row["LocusId"], row["VariantId"]))
        if father_row is None or mother_row is None:
            print(f"WARNING: skipping {row[sample_id_column]} (father: {row.father_id}, mother: {row.mother_id}) {row.VariantId} because table is missing the "
                  f"{'father genotype' if father_row is None else ''} "
                  f"{'and' if father_row is None and mother_row is None else ''} "
                  f"{'mother genotype' if mother_row is None else ''}")
            other_rows.append(row)
            continue

        trio_ids.add((row[sample_id_column], row["father_id"], row["mother_id"]))
        trio_rows.append((row, father_row, mother_row))
        calls_counter += 1

    print(f"Processed {calls_counter} calls in {len(trio_ids)} trios")

    return trio_rows, other_rows


def intervals_overlap(i1, i2):
    """Returns True if Interval i1 overlaps Interval i2. The intervals are considered to be closed, so the intervals
    still overlap if i1.end == i2.begin or i2.end == i1.begin.
    """

    return not (i1.end < i2.begin or i2.end < i1.begin)


def compute_min_distance_mendelian(proband_allele, parent_alleles):
    """Commute the smallest distance between the given proband STR expansion size, and parental STR expansion  size.

    Args:
        proband_allele (int): the proband's allele length.
        parent_alleles (list of allele sizes): list of parental allele lengths.

    Return:
          int: the smallest distance (in base-pairs) between one of the parent_alleles, and the proband_allele.
    """

    return min([abs(int(proband_allele) - int(pa)) for pa in parent_alleles])


def compute_min_distance_mendelian_ci(proband_CI, parent_CIs):
    """Commute the smallest distance between the given proband confidence interval, and the confidence intervals of
    parental genotypes.

    Args:
        proband_CI (Interval): ExpansionHunter genotype confidence interval.
        parent_CIs (list of Intervals): list of ExpansionHunter genotype confidence intervals.

    Return:
          int: the smallest distance (in base-pairs) between one of the Interval in the parent_CIs Interval list, and
          the given proband_CI.
    """

    return min([abs(proband_CI.distance_to(parent_CI)) for parent_CI in parent_CIs])


def is_mendelian_violation(proband_alleles, father_alleles, mother_alleles, is_chrX_locus=False):
    if len(proband_alleles) == 1:
        # AFAIK ExpansionHunter only outputs a haploid genotype (eg. len(proband_alleles) == 1) when the proband is male
        # and the locus is on the X chromosome. In that case, the male chrX allele should always be inherited from the mother.
        if is_chrX_locus:
            ok_mendelian = proband_alleles[0] in mother_alleles
            distance_mendelian = compute_min_distance_mendelian(proband_alleles[0], mother_alleles)
        else:
            # not sure if it's possible to have a haploid genotype that's not on chrX, but leave this in just in case
            ok_mendelian = proband_alleles[0] in father_alleles or proband_alleles[0] in mother_alleles
            distance_mendelian = min(compute_min_distance_mendelian(proband_alleles[0], father_alleles),
                                     compute_min_distance_mendelian(proband_alleles[0], mother_alleles))

    elif len(proband_alleles) == 2:
        ok_mendelian = (
                (proband_alleles[0] in father_alleles and proband_alleles[1] in mother_alleles) or
                (proband_alleles[1] in father_alleles and proband_alleles[0] in mother_alleles))
        distance_mendelian = min(
            compute_min_distance_mendelian(proband_alleles[0], father_alleles) + compute_min_distance_mendelian(proband_alleles[1], mother_alleles),
            compute_min_distance_mendelian(proband_alleles[1], father_alleles) + compute_min_distance_mendelian(proband_alleles[0], mother_alleles),
        )

    else:
        raise ValueError(f"Unexpected proband_alleles value: {proband_alleles}")

    return ok_mendelian, distance_mendelian


def is_mendelian_violation_with_CI(proband_CIs, father_CIs, mother_CIs, is_chrX_locus=False):
    if len(proband_CIs) == 1:
        # See comment about male probands and chrX in the is_mendelian_violation method
        if is_chrX_locus:
            ok_mendelian_ci = any([intervals_overlap(proband_CIs[0], i) for i in mother_CIs])
            distance_mendelian_ci = compute_min_distance_mendelian_ci(proband_CIs[0], mother_CIs)
        else:
            # not sure if it's possible to have a haploid genotype that's not on chrX, but leave this in just in case
            ok_mendelian_ci = (
                any([intervals_overlap(proband_CIs[0], i) for i in father_CIs])) or (
                any([intervals_overlap(proband_CIs[0], i) for i in mother_CIs]))

            distance_mendelian_ci = min(
                compute_min_distance_mendelian_ci(proband_CIs[0], father_CIs),
                compute_min_distance_mendelian_ci(proband_CIs[0], mother_CIs))

    elif len(proband_CIs) == 2:
        ok_mendelian_ci = (
                (any([intervals_overlap(proband_CIs[0], i) for i in father_CIs]) and any([intervals_overlap(proband_CIs[1], i)for i in mother_CIs])) or
                (any([intervals_overlap(proband_CIs[1], i) for i in father_CIs]) and any([intervals_overlap(proband_CIs[0], i) for i in mother_CIs])))
        distance_mendelian_ci = min(
            compute_min_distance_mendelian_ci(proband_CIs[0], father_CIs) + compute_min_distance_mendelian_ci(proband_CIs[1], mother_CIs),
            compute_min_distance_mendelian_ci(proband_CIs[1], father_CIs) + compute_min_distance_mendelian_ci(proband_CIs[0], mother_CIs),
        )
    else:
        raise ValueError(f"Unexpected proband_CIs value: {proband_CIs}")

    return ok_mendelian_ci, distance_mendelian_ci


def get_nearest_parental_allele(proband_allele, parent_alleles):
    """Return the parental allele that's closest in size to the given proband allele.

    Args:
        proband_allele (int): the proband's allele length.
        parent_alleles (list of allele sizes): list of parental allele lengths.

    Return:
        (int, int): the parental allele that's closest in size to the given proband allele.
    """
    nearest_parental_allele = min(parent_alleles, key=lambda x: abs(int(proband_allele) - int(x)))
    difference_between_parent_and_proband_allele_size = abs(int(proband_allele) - int(nearest_parental_allele))
    return nearest_parental_allele, difference_between_parent_and_proband_allele_size


def determine_transmitted_alleles(proband_alleles, father_alleles, mother_alleles, is_chrX_locus=False):
    father_transmitted_allele = mother_transmitted_allele = proband_num_repeats_from_father = proband_num_repeats_from_mother = None
    if len(proband_alleles) == 1:
        # See comment about male probands and chrX in the is_mendelian_violation method
        if is_chrX_locus:
            nearest_maternal_allele = get_nearest_parental_allele(proband_alleles[0], mother_alleles)
            mother_transmitted_allele = nearest_maternal_allele
            proband_num_repeats_from_mother = proband_alleles[0]
        else:
            # not sure if it's possible to have a haploid genotype that's not on chrX, but leave this in just in case
            nearest_paternal_allele, _ = get_nearest_parental_allele(proband_alleles[0], father_alleles)
            nearest_maternal_allele, _ = get_nearest_parental_allele(proband_alleles[0], mother_alleles)
            is_proband_allele_closer_to_paternal_than_to_maternal_allele = abs(int(nearest_paternal_allele) - int(proband_alleles[0])) < abs(int(nearest_maternal_allele) - int(proband_alleles[0]))
            if is_proband_allele_closer_to_paternal_than_to_maternal_allele:
                father_transmitted_allele = nearest_paternal_allele
                proband_num_repeats_from_father = proband_alleles[0]
            else:
                mother_transmitted_allele = nearest_paternal_allele
                proband_num_repeats_from_mother = proband_alleles[0]

    elif len(proband_alleles) == 2:
        nearest_paternal_allele_to_proband_allele1, diff_p1 = get_nearest_parental_allele(proband_alleles[0], father_alleles)
        nearest_maternal_allele_to_proband_allele1, diff_m1 = get_nearest_parental_allele(proband_alleles[0], mother_alleles)
        nearest_paternal_allele_to_proband_allele2, diff_p2 = get_nearest_parental_allele(proband_alleles[1], father_alleles)
        nearest_maternal_allele_to_proband_allele2, diff_m2 = get_nearest_parental_allele(proband_alleles[1], mother_alleles)

        if diff_m1 + diff_p2 < diff_p1 + diff_m2:
            # proband allele1 came from the mother
            mother_transmitted_allele = nearest_maternal_allele_to_proband_allele1
            proband_num_repeats_from_mother = proband_alleles[0]
            # proband allele2 came from the father
            father_transmitted_allele = nearest_paternal_allele_to_proband_allele2
            proband_num_repeats_from_father = proband_alleles[1]
        else:
            # proband allele2 came from the mother
            mother_transmitted_allele = nearest_maternal_allele_to_proband_allele2
            proband_num_repeats_from_mother = proband_alleles[1]
            # proband allele1 came from the father
            father_transmitted_allele = nearest_paternal_allele_to_proband_allele1
            proband_num_repeats_from_father = proband_alleles[0]
    else:
        raise ValueError(f"Unexpected proband_alleles value: {proband_alleles}")

    return father_transmitted_allele, mother_transmitted_allele, proband_num_repeats_from_father, proband_num_repeats_from_mother


def compute_mendelian_violations(trio_rows, sample_id_column="SampleId"):
    """Compute mendelian violations using both exact genotypes and intervals"""

    counters = collections.defaultdict(int)
    counters_ci = collections.defaultdict(int)
    results = []
    results_ci = []

    results_rows = []
    for proband_row, father_row, mother_row in tqdm.tqdm(trio_rows, unit=" variants with no missing genotypes"):
        locus_id = proband_row["LocusId"]
        proband_alleles = proband_row["Genotype"].split("/")   # Num Repeats: Allele 1
        father_alleles = father_row["Genotype"].split("/")
        mother_alleles = mother_row["Genotype"].split("/")

        assert len(proband_alleles) in {1, 2}, proband_alleles
        assert len(father_alleles) in {1, 2}, (father_alleles, father_row)
        assert len(mother_alleles) in {1, 2}, (mother_alleles, mother_row)

        proband_CIs = [Interval(*[int(i) for i in ci.split("-")]) for ci in proband_row["GenotypeConfidenceInterval"].split("/")]
        father_CIs = [Interval(*[int(i) for i in ci.split("-")]) for ci in father_row["GenotypeConfidenceInterval"].split("/")]
        mother_CIs = [Interval(*[int(i) for i in ci.split("-")]) for ci in mother_row["GenotypeConfidenceInterval"].split("/")]

        is_chrX_locus = "X" in proband_row["ReferenceRegion"]
        ok_mendelian, distance_mendelian = is_mendelian_violation(proband_alleles, father_alleles, mother_alleles, is_chrX_locus=is_chrX_locus)
        ok_mendelian_ci, distance_mendelian_ci = is_mendelian_violation_with_CI(proband_CIs, father_CIs, mother_CIs, is_chrX_locus=is_chrX_locus)

        father_transmitted_allele, mother_transmitted_allele, proband_num_repeats_from_father, proband_num_repeats_from_mother = determine_transmitted_alleles(
            proband_alleles, father_alleles, mother_alleles, is_chrX_locus=is_chrX_locus)

        if ok_mendelian:
            assert distance_mendelian == 0, f"{locus_id}  d:{distance_mendelian}  ({father_row.Genotype} + {mother_row.Genotype} => {proband_row.Genotype})"

        mendelian_results_string = f"{locus_id}  d:{distance_mendelian}  ({father_row.Genotype} + {mother_row.Genotype} => {proband_row.Genotype}) {proband_row[sample_id_column]}  {father_row[sample_id_column]}  {mother_row[sample_id_column]}    {proband_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg {father_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg {mother_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg"
        if not ok_mendelian:
            results += [mendelian_results_string]
            counters[f"{locus_id} ({proband_row.RepeatUnit})"] += 1

        mendelian_ci_results_string = f"{locus_id}   ({father_row.GenotypeConfidenceInterval} + {mother_row.GenotypeConfidenceInterval} => {proband_row.GenotypeConfidenceInterval})  {proband_row[sample_id_column]}  {father_row[sample_id_column]}  {mother_row[sample_id_column]}  {proband_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg {father_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg {mother_row[sample_id_column]}*_ExpansionHunter4/*{locus_id}*.svg"
        if not ok_mendelian_ci:
            results_ci += [mendelian_ci_results_string]
            counters_ci[f"{locus_id} ({proband_row.RepeatUnit})"] += 1

        #assert not (ok_mendelian and not ok_mendelian_ci)  # it should never be the case that mendelian inheritance is consistent for exact genotypes, and not consistent for CI interval-overlap.
        _, reference_region_start_0based, reference_region_end_1based = parse_interval(proband_row["ReferenceRegion"])
        if (reference_region_end_1based - reference_region_start_0based) % len(proband_row["RepeatUnit"]):
            print(f"WARNING: {proband_row.ReferenceRegion} is not a multiple of the repeat unit size ({len(proband_row.RepeatUnit)})")

        repeats_in_reference = str(int((reference_region_end_1based - reference_region_start_0based) / len(proband_row["RepeatUnit"])))
        proband_is_homozygous_reference = all(proband_allele == repeats_in_reference for proband_allele in proband_alleles)
        mother_is_homozygous_reference = all(mother_allele == repeats_in_reference for mother_allele in mother_alleles)
        father_is_homozygous_reference = all(father_allele == repeats_in_reference for father_allele in father_alleles)

        all_genotypes_are_the_same = set(proband_alleles) == set(mother_alleles) and set(proband_alleles) == set(father_alleles)
        all_genotypes_are_homozygous_reference = proband_is_homozygous_reference and mother_is_homozygous_reference and father_is_homozygous_reference

        results_row = {
            'LocusId': f"{locus_id} ({proband_row.VariantId})",
            'ReferenceRegion': proband_row["ReferenceRegion"],
            'VariantId': proband_row["VariantId"],
            'RepeatUnit': proband_row["RepeatUnit"],
            'RepeatUnitLength': len(proband_row["RepeatUnit"]),
            'IsMendelianViolation': not ok_mendelian,
            'IsMendelianViolationCI': not ok_mendelian_ci,
            'MendelianViolationDistance': distance_mendelian,
            'MendelianViolationDistanceCI': distance_mendelian_ci,

            'MendelianViolationSummary': 'MV-CI!' if not ok_mendelian_ci else ('MV' if not ok_mendelian else 'ok'),

            'ProbandGenotype': proband_row["Genotype"],
            'ProbandGenotypeCI': proband_row["GenotypeConfidenceInterval"],
            'ProbandGenotypeCI_size': proband_CIs[-1].length(),

            'FatherGenotype': father_row["Genotype"],
            'FatherGenotypeCI': father_row["GenotypeConfidenceInterval"],

            'MotherGenotype': mother_row["Genotype"],
            'MotherGenotypeCI': mother_row["GenotypeConfidenceInterval"],

            'AllGenotypesAreTheSame': all_genotypes_are_the_same,
            'AllGenotypesAreHomozygousReference': all_genotypes_are_homozygous_reference,

            'ProbandSampleId': proband_row[sample_id_column],
            'FatherSampleId': father_row[sample_id_column],
            'MotherSampleId': mother_row[sample_id_column],
            'ProbandSex': proband_row["Sex"],

            'ProbandNumRepeatsAllele2': proband_row["Num Repeats: Allele 2"],
            'FatherNumRepeatsAllele2 ': father_row["Num Repeats: Allele 2"],
            'MotherNumRepeatsAllele2': mother_row["Num Repeats: Allele 2"],

            'ProbandCoverage': proband_row["Coverage"],
            'FatherCoverage': father_row["Coverage"],
            'MotherCoverage': mother_row["Coverage"],
            'MinCoverage': min(float(proband_row["Coverage"]), float(father_row["Coverage"]), float(mother_row["Coverage"])),

            'MendelianResultsSummary': mendelian_results_string,
            'MendelianCIResultsSummary': mendelian_ci_results_string,

            'FatherTransmittedAllele': father_transmitted_allele,
            'MotherTransmittedAllele': mother_transmitted_allele,
            'ProbandNumRepeatsFromFather': proband_num_repeats_from_father,
            'ProbandNumRepeatsFromMother': proband_num_repeats_from_mother,
        }

        existing_column_names = set(proband_row.to_dict().keys()) & set(father_row.to_dict().keys()) & set(mother_row.to_dict().keys())
        if all(k in existing_column_names for k in EXTRA_INPUT_COLUMNS):
           results_row.update({
               'ProbandNumSpanningReads': proband_row["NumSpanningReads"],
               'ProbandNumFlankingReads': proband_row["NumFlankingReads"],
               'ProbandNumInrepeatReads': proband_row["NumInrepeatReads"],
               'FatherNumSpanningReads': father_row["NumSpanningReads"],
               'FatherNumFlankingReads': father_row["NumFlankingReads"],
               'FatherNumInrepeatReads': father_row["NumInrepeatReads"],
               'MotherNumSpanningReads': mother_row["NumSpanningReads"],
               'MotherNumFlankingReads': mother_row["NumFlankingReads"],
               'MotherNumInrepeatReads': mother_row["NumInrepeatReads"],

               'ProbandNumAllelesSupportedBySpanningReads': int(proband_row["NumAllelesSupportedBySpanningReads"]) + int(proband_row["NumAllelesSupportedByFlankingReads"]) + int(proband_row["NumAllelesSupportedByInrepeatReads"]),
               'FatherNumAllelesSupportedByFlankingReads': int(father_row["NumAllelesSupportedBySpanningReads"]) + int(father_row["NumAllelesSupportedByFlankingReads"]) + int(father_row["NumAllelesSupportedByInrepeatReads"]),
               'MotherNumAllelesSupportedByInrepeatReads': int(mother_row["NumAllelesSupportedBySpanningReads"]) + int(mother_row["NumAllelesSupportedByFlankingReads"]) + int(mother_row["NumAllelesSupportedByInrepeatReads"]),
           })

        results_rows.append(results_row)

        #if ok_mendelian_ci and not ok_mendelian:
        #    print("    " + mendelian_results_string)
        #    print("ci: " + mendelian_ci_results_string)

        #print(results_ci[-1])

    for name, c in sorted(counters.items(), key=lambda t: -t[1]):
        print(f"{c:5d}  ({100*float(c)/len(trio_rows):0.0f}%) {name}")

    print("\n".join(sorted(results)))

    for name, c in sorted(counters_ci.items(), key=lambda t: -t[1]):
        print(f"{c:5d}  ({100*float(c)/len(trio_rows):0.0f}%) {name}")

    print("\n".join(sorted(results_ci)))

    return pd.DataFrame(results_rows)


def main():
    """Main"""

    args = parse_args()

    combined_str_calls_df = parse_combined_str_calls_tsv_path(args.combined_str_calls_tsv, sample_id_column=args.sample_id_column)
    print(f"Parsed {len(combined_str_calls_df):,d} rows from {args.combined_str_calls_tsv}")
    fam_file_df = parse_fam_file(args.fam_file)
    print(f"Parsed {len(fam_file_df):,d} rows from {args.fam_file}")

    combined_str_calls_df.set_index(args.sample_id_column, inplace=True)
    fam_file_df.set_index("individual_id", inplace=True)

    combined_str_calls_df = combined_str_calls_df.join(fam_file_df, how="left").reset_index().rename(columns={"index": args.sample_id_column})
    #combined_str_calls_df = combined_str_calls_df.fillna(0)
    print(f"Left-joining the .fam file to the combined_str_calls_df table added fam file info to "
          f"{sum(~combined_str_calls_df.father_id.isna() & ~combined_str_calls_df.mother_id.isna()):,d} rows")
    trio_rows, other_rows = group_rows_by_trio(combined_str_calls_df, sample_id_column=args.sample_id_column)
    print(f"Found {len(trio_rows):,d} trio rows and {len(other_rows):,d} other rows in {args.combined_str_calls_tsv}")

    mendelian_violations_df = compute_mendelian_violations(trio_rows, sample_id_column=args.sample_id_column)
    other_rows_df = pd.DataFrame((r.to_dict() for r in other_rows))

    if not args.output_prefix:
        args.output_prefix = re.sub(".tsv(.gz)?$", "", str(args.combined_str_calls_tsv))

    output_tsv_path = f"{args.output_prefix}.mendelian_violations.tsv"
    mendelian_violations_df.to_csv(output_tsv_path, index=False, header=True, sep="\t")
    print(f"Wrote {len(mendelian_violations_df):,d} rows to {output_tsv_path}")

    output_tsv_path = f"{args.output_prefix}.non_trio_rows.tsv"
    other_rows_df.to_csv(output_tsv_path, index=False, header=True, sep="\t")
    print(f"Wrote {len(other_rows_df):,d} rows to {output_tsv_path}")

    if args.output_locus_stats_tsv:
        output_path = f"{args.output_prefix}.locus_stats.tsv"
        # Count how many times is locus has isMendelianViolation == True and also isMendelianViolationCI == True
        mendelian_violations_df.groupby(["LocusId", "ReferenceRegion", "VariantId", "RepeatUnit", "RepeatUnitLength"]).agg({
            "IsMendelianViolation": "sum",
            "IsMendelianViolationCI": "sum",
            "MendelianViolationDistance": "sum",
            "MendelianViolationDistanceCI": "sum",
            "ProbandSampleId": "count",
        }).to_csv(output_path, sep="\t")
        print(f"Wrote locus stats to {output_path}")


if __name__ == "__main__":
    main()
