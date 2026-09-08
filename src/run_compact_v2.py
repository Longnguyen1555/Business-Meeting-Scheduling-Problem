from B2B_Instance import B2BSATModel, read_instance
from B2B_Instance_CompactV2 import B2BSATModelV2


INSTANCE = "data_precedence_stress/forumt-14crafd.prec40.dzn"

inst = read_instance(INSTANCE)


# ============================================================
# CURRENT MODEL
# ============================================================

current_model = B2BSATModel(
    inst,

    domain_mode="reduced",
    domain_filter_graph="distance_closure",

    precedence_encoding="sparse_suffix",
    precedence_graph="distance_closure",

    encoding_variant="imp12+",

    objective_mode="ir",
)

current = current_model.build_base_cnf()


# ============================================================
# COMPACT V2
# ============================================================

v2_model = B2BSATModelV2(
    inst,

    domain_mode="reduced",
    domain_filter_graph="distance_closure",

    precedence_encoding="sparse_suffix",
    precedence_graph="distance_closure",

    encoding_variant="imp12+",

    capacity_mode="slot_cluster",
    capacity_cardinality="seqcounter",
    bg_counter_mode="shared_dp",

    objective_mode="ir",
)

v2 = v2_model.build_base_cnf()


# ============================================================
# RESULTS
# ============================================================

print("\nCURRENT")
print("vars      =", current.n_vars)
print("aux vars  =", current.n_auxiliary_variables)
print("clauses   =", current.n_clauses)
print("literals  =", current.n_hard_literals)

print("\nCOMPACT V2")
print("vars      =", v2.n_vars)
print("aux vars  =", v2.n_auxiliary_variables)
print("clauses   =", v2.n_clauses)
print("literals  =", v2.n_hard_literals)


print("\nREDUCTION")

print(
    "vars      =",
    round(
        100 * (current.n_vars - v2.n_vars)
        / current.n_vars,
        2,
    ),
    "%",
)

print(
    "aux vars  =",
    round(
        100
        * (
            current.n_auxiliary_variables
            - v2.n_auxiliary_variables
        )
        / current.n_auxiliary_variables,
        2,
    ),
    "%",
)

print(
    "clauses   =",
    round(
        100 * (current.n_clauses - v2.n_clauses)
        / current.n_clauses,
        2,
    ),
    "%",
)

print(
    "literals  =",
    round(
        100
        * (
            current.n_hard_literals
            - v2.n_hard_literals
        )
        / current.n_hard_literals,
        2,
    ),
    "%",
)