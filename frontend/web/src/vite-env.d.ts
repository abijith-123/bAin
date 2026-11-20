/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_BAIN_TOKEN_ADMIN: string;
  readonly VITE_BAIN_TOKEN_DOCTOR: string;
  readonly VITE_BAIN_TOKEN_NURSE: string;
  readonly VITE_BAIN_TOKEN_PHARMACIST: string;
  readonly VITE_BAIN_TOKEN_BILLING_SPECIALIST: string;
  readonly VITE_BAIN_TOKEN_TRANSPORT_STAFF: string;
  readonly VITE_BAIN_TOKEN_HOUSEKEEPING: string;
}
