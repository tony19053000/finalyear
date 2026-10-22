"""The ML planner's data side (V0.15, ADR-0021, D333-D344).

G1 (this package's first part) turns recorded decisions and outcomes into a
versioned training dataset:

* :mod:`.schema` - versions, the ordered feature list (with the groups G5
  ablates) and the learnable terms (pure);
* :mod:`.features` - one candidate's features from the decision's replay
  snapshot, missing = NaN (pure);
* :mod:`.targets` - the residual target ``ln(actual / raw)`` per run and term,
  every exclusion counted by reason (pure);
* :mod:`.splits` - group-safe splits (leave one family out, group k-fold) (pure);
* :mod:`.dataset` - rows -> sorted dataset + canonical Parquet + digest (pure,
  needs pyarrow);
* :mod:`.loader` - reads the runs through the calibration's matching (I/O);
* :mod:`.export` - the dataset service the API and CLI share (I/O).

No ML library is imported anywhere in G1 (the planner package never imports
one either - AST tests pin both).
"""
