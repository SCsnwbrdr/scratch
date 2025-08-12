from dataclasses import dataclass
from typing import List, Dict, Any, Literal
import math

BackupMode = Literal["none", "blobs_operational", "blobs_vaulted"]

@dataclass
class Inputs:
    # Core scale inputs
    num_clients: int
    envs_per_client: int
    avg_state_size_mb: float  # average on-disk state size per env including versioning growth
    deployments_per_env_per_year: int  # each deployment ~= 3 reads + 1 write
    pr_runs_per_client_per_year: int   # each PR run triggers 1 read per environment (per client)
    num_regions: int
    storage_accounts_per_region: int

    # Logging from transactions
    log_bytes_per_txn: float = 1024.0  # ~1 KB/op for full diagnostics

    # Backup modeling
    backup_mode: BackupMode = "blobs_operational"
    # For 'blobs_vaulted' mode only:
    # Base monthly protected instance price for Azure Blobs in your region (user-supplied).
    blob_vaulted_pi_price_per_month: float = 0.0
    # Tier percentages per MS pricing doc for Blobs (defaults aligned to current docs):
    # <10 GB => 10% of base; 10-100 GB => 30%; 100 GB-1 TB => 60%; >=1 TB => 100%.
    blob_vaulted_tiers: Dict[str, float] = None  # set in __post_init__

    # Write operations billed by Backup service to the vault (per 10k writes)
    blob_vaulted_write_per_10k_price: float = 0.0
    # Estimated write ops per month per account performed by Backup service (user-supplied; depends on churn/policy).
    blob_vaulted_write_ops_per_month_per_account: float = 0.0
    # Vault storage if used (GB/account/month) and its $/GB-month
    backup_vault_storage_gb_per_account_month: float = 0.0
    backup_vault_storage_price_per_gb_month: float = 0.0

    # Growth & horizon
    growth_rate_clients: float = 0.0     # e.g., 0.2 for +20% YoY client growth
    years: int = 1                       # planning horizon (integer years)
    hours_per_month: float = 730.0       # used for Private Endpoint hourly costs

    def __post_init__(self):
        if self.blob_vaulted_tiers is None:
            # Keys represent bands; values are fraction of base PI monthly price
            self.blob_vaulted_tiers = {
                "<10GB": 0.10,
                "10-100GB": 0.30,
                "100GB-1TB": 0.60,
                ">=1TB": 1.00
            }

@dataclass
class Rates:
    # Storage (Blob, Hot, chosen redundancy) — all prices are USD per unit
    storage_gb_month_price: float

    # Transactions (per 10,000 ops) for Blob Hot tier
    read_txn_per_10k_price: float
    write_txn_per_10k_price: float

    # Private Link / Private Endpoint
    private_endpoint_hour_price: float        # per endpoint-hour
    private_link_data_price_per_gb: float     # per GB processed via PE (both directions)

    # Log Analytics
    log_ingestion_price_per_gb: float

def _clients_in_year(base_clients: int, growth_rate: float, year_index: int) -> int:
    """Ceiling of clients after compounding growth for year_index (0-based)."""
    return math.ceil(base_clients * ((1 + growth_rate) ** year_index))

def _blobs_vaulted_pi_monthly_fee(per_account_size_gb: float, base_monthly: float, tiers: Dict[str, float]) -> float:
    """Protected instance monthly fee per storage account for Blobs vaulted backup."""
    if per_account_size_gb < 10:
        return base_monthly * tiers.get("<10GB", 0.0)
    elif per_account_size_gb < 100:
        return base_monthly * tiers.get("10-100GB", 0.0)
    elif per_account_size_gb < 1024:
        return base_monthly * tiers.get("100GB-1TB", 0.0)
    else:
        return base_monthly * tiers.get(">=1TB", 1.0)

def estimate_backend_costs(inputs: Inputs, rates: Rates) -> Dict[str, Any]:
    """
    Estimate yearly cost breakdown for Terraform AzureRM backends across a multi-region setup.

    Model notes:
    - Environments are GLOBAL per client (not per region). Clients/envs scale transactions and stored GB,
      but do NOT multiply fixed infra counts in each region.
    - Fixed infra (Private Endpoints) is determined solely by num_regions * storage_accounts_per_region.
    - Log ingestion is derived from transaction volume using `log_bytes_per_txn` and evenly distributed across accounts.
    - Backup costs:
        * 'none' => no backup line items.
        * 'blobs_operational' => no protected instance fee; no vault storage; (operational features cost is in storage/ops, not modeled here).
        * 'blobs_vaulted' => protected instance fee per account using tiered % of a region-specific base price;
          optional vault storage and backup write-ops charges.
    """
    breakdown_by_year = []

    total_accounts = inputs.num_regions * inputs.storage_accounts_per_region
    avg_state_size_gb = inputs.avg_state_size_mb / 1024.0

    for y in range(inputs.years):
        clients_y = _clients_in_year(inputs.num_clients, inputs.growth_rate_clients, y)

        # ---------- ENVIRONMENTS (GLOBAL) ----------
        total_envs = clients_y * inputs.envs_per_client

        # ---------- Storage GB-month ----------
        total_state_gb = total_envs * avg_state_size_gb
        storage_cost_year = total_state_gb * rates.storage_gb_month_price * 12.0

        # ---------- Transactions ----------
        reads_per_env = (3 * inputs.deployments_per_env_per_year) + inputs.pr_runs_per_client_per_year
        writes_per_env = (1 * inputs.deployments_per_env_per_year)
        total_reads = reads_per_env * total_envs
        total_writes = writes_per_env * total_envs
        total_txns = total_reads + total_writes

        read_cost_year = (total_reads / 10000.0) * rates.read_txn_per_10k_price
        write_cost_year = (total_writes / 10000.0) * rates.write_txn_per_10k_price
        txn_cost_year = read_cost_year + write_cost_year

        # ---------- Private Link / Endpoint (fixed by account count) ----------
        pe_hours_year = total_accounts * inputs.hours_per_month * 12.0
        pe_hour_cost_year = pe_hours_year * rates.private_endpoint_hour_price
        data_processed_gb_year = total_txns * avg_state_size_gb
        pe_data_cost_year = data_processed_gb_year * rates.private_link_data_price_per_gb
        private_link_cost_year = pe_hour_cost_year + pe_data_cost_year

        # ---------- Log Analytics ingestion (derived from txns) ----------
        if total_accounts > 0:
            txns_per_month_all = total_txns / 12.0
            txns_per_month_per_account = txns_per_month_all / total_accounts
            log_gb_per_account_per_month = (txns_per_month_per_account * inputs.log_bytes_per_txn) / (1024.0 ** 3)
            log_ingestion_gb_year = total_accounts * log_gb_per_account_per_month * 12.0
        else:
            log_gb_per_account_per_month = 0.0
            log_ingestion_gb_year = 0.0
        log_ingestion_cost_year = log_ingestion_gb_year * rates.log_ingestion_price_per_gb

        # ---------- Backup ----------
        backup = {
            "mode": inputs.backup_mode,
            "per_account_size_gb": 0.0,
            "pi_monthly_fee_per_account": 0.0,
            "instance_cost_year": 0.0,
            "vault_storage_cost_year": 0.0,
            "backup_write_ops_year": 0.0,
            "backup_write_ops_cost_year": 0.0,
        }

        if inputs.backup_mode != "none" and total_accounts > 0:
            per_account_size_gb = total_state_gb / total_accounts if total_accounts > 0 else 0.0
            backup["per_account_size_gb"] = per_account_size_gb

            if inputs.backup_mode == "blobs_vaulted":
                # Protected instance fee per account per month (tiered % of base)
                pi_monthly = _blobs_vaulted_pi_monthly_fee(
                    per_account_size_gb,
                    inputs.blob_vaulted_pi_price_per_month,
                    inputs.blob_vaulted_tiers,
                )
                backup["pi_monthly_fee_per_account"] = pi_monthly
                backup["instance_cost_year"] = pi_monthly * total_accounts * 12.0

                # Optional vault storage
                backup["vault_storage_cost_year"] = (
                    inputs.backup_vault_storage_gb_per_account_month
                    * inputs.backup_vault_storage_price_per_gb_month
                    * total_accounts * 12.0
                )

                # Backup write operations billed per 10,000 writes
                yearly_backup_writes = inputs.blob_vaulted_write_ops_per_month_per_account * total_accounts * 12.0
                backup["backup_write_ops_year"] = yearly_backup_writes
                backup["backup_write_ops_cost_year"] = (yearly_backup_writes / 10000.0) * inputs.blob_vaulted_write_per_10k_price

            # blobs_operational => no PI fee; no vault storage (operational features billed under storage, not here)

        # ---------- Year assembly ----------
        year_breakdown = {
            "year_index": y + 1,
            "clients": clients_y,
            "environments_global": total_envs,
            "storage_accounts": total_accounts,
            "avg_state_size_gb": round(avg_state_size_gb, 6),
            "storage_gb": round(total_state_gb, 6),
            "transactions": {
                "reads": int(total_reads),
                "writes": int(total_writes),
                "total": int(total_txns),
                "read_cost": read_cost_year,
                "write_cost": write_cost_year,
                "total_txn_cost": txn_cost_year
            },
            "private_link": {
                "endpoint_hours": pe_hours_year,
                "data_processed_gb": data_processed_gb_year,
                "endpoint_hour_cost": pe_hour_cost_year,
                "data_cost": pe_data_cost_year,
                "total_pl_cost": private_link_cost_year
            },
            "logging": {
                "log_bytes_per_txn": inputs.log_bytes_per_txn,
                "gb_per_account_per_month": log_gb_per_account_per_month,
                "ingestion_gb_year": log_ingestion_gb_year,
                "ingestion_cost_year": log_ingestion_cost_year
            },
            "backup": backup,
            "storage_cost_year": storage_cost_year,
        }

        # Year total
        year_total = (
            storage_cost_year
            + txn_cost_year
            + private_link_cost_year
            + log_ingestion_cost_year
        )

        # Add backup costs depending on mode
        if inputs.backup_mode == "blobs_vaulted":
            year_total += backup["instance_cost_year"] + backup["vault_storage_cost_year"] + backup["backup_write_ops_cost_year"]

        # blobs_operational/none add nothing here

        year_breakdown["year_total_cost"] = year_total
        breakdown_by_year.append(year_breakdown)

    grand_total = sum(y["year_total_cost"] for y in breakdown_by_year)

    return {
        "by_year": breakdown_by_year,
        "grand_total": grand_total
    }
