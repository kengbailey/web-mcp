terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# --- APIs ---
resource "google_project_service" "compute" {
  service                    = "compute.googleapis.com"
  disable_dependent_services = false
}

resource "google_project_service" "secretmanager" {
  service                    = "secretmanager.googleapis.com"
  disable_dependent_services = false
}

# --- Network ---
resource "google_compute_network" "webintel" {
  name                    = "webintel-net"
  auto_create_subnetworks = true
}

# SSH only via IAP TCP forwarding (35.235.240.0/20). No public SSH.
# The MCP port (3090) is bound to 127.0.0.1 on the VM; cloudflared reaches it
# locally, so no inbound rule is needed for it.
resource "google_compute_firewall" "ssh_iap" {
  name    = "webintel-allow-iap-ssh"
  network = google_compute_network.webintel.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["35.235.240.0/20"]
}

# --- Static external IP (stable datacenter egress IP for scraping) ---
resource "google_compute_address" "webintel" {
  name = "webintel-ip"
}

# --- Service account ---
resource "google_service_account" "webintel" {
  account_id   = "webintel-vm"
  display_name = "WebIntel MCP VM"
}

# Project-level secretAccessor removed 2026-10-08 (security audit): it let this
# VM read every secret in the project, including other services'. Access is
# granted per secret below (google_secret_manager_secret_iam_member.webintel).

# --- Secrets (empty containers; values seeded out-of-band via seed-secrets.sh
# so secret values never enter Terraform state) ---
locals {
  secret_names = [
    "webintel-mcp-authkit-domain",
    "webintel-mcp-base-url",
    "webintel-mcp-resource-url",
    "webintel-mcp-machine-secret",
    "webintel-mcp-machine-issuer",
    "webintel-mcp-required-scopes",
    "webintel-reddit-client-id",
    "webintel-reddit-client-secret",
    "webintel-reddit-user-agent",
    "webintel-youtube-proxy-url",
    "webintel-stt-endpoint",
    "webintel-stt-model",
    "webintel-stt-api-key",
    "webintel-exa-api-key",
    "webintel-youtube-api-key",
    "webintel-cloudflare-tunnel-token",
  ]
}

resource "google_secret_manager_secret" "webintel" {
  for_each   = toset(local.secret_names)
  secret_id  = each.value
  depends_on = [google_project_service.secretmanager]

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "webintel" {
  for_each  = google_secret_manager_secret.webintel
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.webintel.email}"
}

# --- VM ---
resource "google_compute_instance" "webintel" {
  name         = "webintel-mcp"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["webintel"]

  boot_disk {
    initialize_params {
      image = "ubuntu-2204-lts"
      size  = 30
    }
  }

  network_interface {
    network = google_compute_network.webintel.name
    access_config {
      nat_ip = google_compute_address.webintel.address
    }
  }

  service_account {
    email  = google_service_account.webintel.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
    repo-ref       = var.repo_ref
  }

  metadata_startup_script = file("${path.module}/scripts/vm-startup.sh")

  lifecycle {
    # metadata_startup_script forces replacement when it changes. Script edits are pushed
    # in place instead (deploy/README.md):
    #   gcloud compute instances add-metadata webintel-mcp --zone us-central1-a \
    #     --metadata-from-file startup-script=deploy/terraform/scripts/vm-startup.sh
    ignore_changes = [metadata_startup_script]
  }
}
