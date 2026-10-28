
import pandas as pd
from pathlib import Path

data_file = Path("gleif_sample_1000.csv")

df = pd.read_csv(data_file)

print("\nDATASET SHAPE")
print(df.shape)

print("\nFIRST FIVE RECORDS")
print(df.head().to_string(index=False))

print("\nTOP 10 JURISDICTIONS")
print(df["jurisdiction"].value_counts().head(10))

print("\nMISSING VALUES")
print(df.isna().sum())

print("\nDUPLICATE LEIs")
print(df["lei"].duplicated().sum())
