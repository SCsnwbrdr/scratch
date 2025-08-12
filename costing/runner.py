# run_costs.py
from azurerm_backend_costs import Inputs, Rates, estimate_backend_costs
import json

rates = Rates(
    storage_gb_month_price=0.018,          # placeholder
    read_txn_per_10k_price=0.004,          # placeholder
    write_txn_per_10k_price=0.05,          # placeholder
    private_endpoint_hour_price=0.01,      # placeholder
    private_link_data_price_per_gb=0.01,   # placeholder
    log_ingestion_price_per_gb=2.76,       # placeholder
)

inputs = Inputs(
    num_clients=100,
    envs_per_client=3.5,
    avg_state_size_mb=1,
    deployments_per_env_per_year=200,
    pr_runs_per_client_per_year=16,
    num_regions=3,
    storage_accounts_per_region=2,
    log_bytes_per_txn=1024.0,
    growth_rate_clients=0.25,
    years=5,
    backup_mode="blobs_vaulted",
    blob_vaulted_pi_price_per_month=8.0,             # placeholder base PI $/mo for blobs (set real value)
    blob_vaulted_write_per_10k_price=0.057,          # example LRS write per 10k (set real for region)
    blob_vaulted_write_ops_per_month_per_account=5000, # placeholder ops count
    backup_vault_storage_gb_per_account_month=10.0,  # placeholder
    backup_vault_storage_price_per_gb_month=0.023    # placeholder LRS
)
print("Estimating costs with inputs:")

result = estimate_backend_costs(inputs, rates)
print(json.dumps(result, indent=2))

# Optional: quick summary
gt = result["grand_total"]
by_year = [(y["year_index"], y["year_total_cost"]) for y in result["by_year"]]
print("\nYear totals:", ", ".join([f"Y{yr}: ${tot:,.2f}" for yr, tot in by_year]))
print(f"Grand total: ${gt:,.2f}")
