import argparse
import hashlib
import secrets
import sys
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.db.session import get_sync_db, sync_engine
from app.db.models import APIKey, Base

def generate_credentials() -> tuple[str, str, str]:
    """Generates client_id, client_secret, and secret_hash."""
    client_id = f"client_{secrets.token_hex(12)}"
    client_secret = f"secret_{secrets.token_urlsafe(32)}"
    secret_hash = hashlib.sha256(client_secret.encode("utf-8")).hexdigest()
    return client_id, client_secret, secret_hash

def main():
    parser = argparse.ArgumentParser(description="Generate Client ID and Secret for alphasync-data-layer.")
    parser.add_argument("--owner", required=True, help="Name of the API client owner (e.g., 'alphasync-website')")
    parser.add_argument(
        "--scopes", 
        default="nse:eq,bse:eq,nse:fut,nse:opt,mcx:fut,admin", 
        help="Comma-separated list of scopes (e.g., 'nse:eq,bse:eq,nse:fut,nse:opt,mcx:fut,admin')"
    )
    parser.add_argument("--rate-limit", type=int, default=60, help="Rate limit in requests per minute (default: 60)")

    args = parser.parse_args()

    scopes_list = [s.strip() for s in args.scopes.split(",") if s.strip()]

    # Ensure database tables exist
    Base.metadata.create_all(bind=sync_engine)

    client_id, client_secret, secret_hash = generate_credentials()

    # Store credentials in database
    with get_sync_db() as db:
        # Check if client_id already exists (extremely unlikely)
        existing = db.query(APIKey).filter(APIKey.client_id == client_id).first()
        if existing:
            print("Error: Generated a duplicate Client ID. Please try again.")
            sys.exit(1)

        api_key_obj = APIKey(
            client_id=client_id,
            secret_hash=secret_hash,
            owner=args.owner,
            scopes=scopes_list,
            rate_limit_per_min=args.rate_limit,
            is_active=True
        )
        db.add(api_key_obj)
        db.commit()
        db.refresh(api_key_obj)

    print("=" * 60)
    print("CLIENT CREDENTIALS GENERATED SUCCESSFULLY")
    print("=" * 60)
    print(f"Owner:         {args.owner}")
    print(f"ID (DB Ref):   {api_key_obj.id}")
    print(f"Scopes:        {api_key_obj.scopes}")
    print(f"Rate Limit:    {api_key_obj.rate_limit_per_min} req/min")
    print("-" * 60)
    print(f"CLIENT ID (API KEY):")
    print(f"\n{client_id}\n")
    print("-" * 60)
    print(f"CLIENT SECRET (Copy this now! It will NEVER be shown again):")
    print(f"\n{client_secret}\n")
    print("=" * 60)

if __name__ == "__main__":
    main()
