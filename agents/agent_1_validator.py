import pandas as pd
from config import OD_COLUMNS

class InputValidator:
    def __init__(self):
        pass

    def validate_od_data(self, df):
        """
        Validates the OD Traffic Excel data.
        Returns a cleaned dataframe.
        """
        # If the first row contains the actual headers (common when exported from internal systems)
        if not df.empty and str(df.columns[0]).startswith('Unnamed:') and ('FROMSTTN' in df.iloc[0].values or 'From Station Code' in df.iloc[0].values):
            df.columns = df.iloc[0]
            df = df.drop(0).reset_index(drop=True)

        # Handle alternative column names from different formats
        column_map = {
            'FROMSTTN': 'From Station Code',
            'FROMNAME': 'From Station Name',
            'TOSTTN': 'To Station Code',
            'TONAME': 'To Station Name',
            'No.of Rakes/No. of Units': 'No. of Rakes / Wagon Units'
        }
        df = df.rename(columns=column_map)
        
        # Remove duplicated columns (keep first) to prevent DataFrame attribute errors
        df = df.loc[:, ~df.columns.duplicated()]
        
        # If Commodity is missing in the alternative format, just fill it with UNKNOWN
        if 'Commodity' not in df.columns:
            df['Commodity'] = 'UNKNOWN'

        # Check required columns
        missing_cols = [col for col in OD_COLUMNS if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Missing required columns in OD Data: {missing_cols}")

        # Drop rows where From or To Station Code is empty
        clean_df = df.dropna(subset=['From Station Code', 'To Station Code']).copy()

        # Clean strings
        clean_df['From Station Code'] = clean_df['From Station Code'].astype(str).str.strip().str.upper()
        clean_df['To Station Code'] = clean_df['To Station Code'].astype(str).str.strip().str.upper()

        # Remove duplicate rows
        initial_count = len(clean_df)
        clean_df.drop_duplicates(inplace=True)
        dropped_duplicates = initial_count - len(clean_df)

        # Fill NaNs in numeric columns
        if 'Annual Tonnage' in clean_df.columns:
            # Handle possible string formatting with commas (e.g. "12,345")
            clean_df['Annual Tonnage'] = clean_df['Annual Tonnage'].astype(str).str.replace(',', '', regex=False)
            clean_df['Annual Tonnage'] = pd.to_numeric(clean_df['Annual Tonnage'], errors='coerce').fillna(0)
            
        if 'No. of Rakes / Wagon Units' in clean_df.columns:
            clean_df['No. of Rakes / Wagon Units'] = clean_df['No. of Rakes / Wagon Units'].astype(str).str.replace(',', '', regex=False)
            clean_df['No. of Rakes / Wagon Units'] = pd.to_numeric(clean_df['No. of Rakes / Wagon Units'], errors='coerce').fillna(0)
            
        print(f"Validated OD Data. Dropped {dropped_duplicates} duplicates. Total valid rows: {len(clean_df)}")
        return clean_df

    def validate_dfc_stations(self, df):
        """
        Validates DFC Stations data.
        """
        required_cols = ['DFC Station Code', 'Latitude', 'Longitude']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Missing required columns in DFC Stations Data: {missing_cols}")

        clean_df = df.dropna(subset=required_cols).copy()
        clean_df['DFC Station Code'] = clean_df['DFC Station Code'].astype(str).str.strip().str.upper()
        clean_df['Latitude'] = pd.to_numeric(clean_df['Latitude'], errors='coerce')
        clean_df['Longitude'] = pd.to_numeric(clean_df['Longitude'], errors='coerce')
        clean_df = clean_df.dropna(subset=['Latitude', 'Longitude'])
        return clean_df
