-- Canonical MySQL 8.4 schema for the standalone iDox refund workflow.
-- Database creation is deliberately owned by `python -m db.admin create`.
-- Keep these tables in dependency order so a fresh schema can be applied
-- without disabling foreign-key checks.

CREATE TABLE customers (
  customer_id VARCHAR(36) NOT NULL,
  email VARCHAR(255) NOT NULL,
  full_name VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (customer_id),
  UNIQUE KEY uq_customers_email (email),
  KEY idx_customers_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE orders (
  order_id VARCHAR(36) NOT NULL,
  customer_id VARCHAR(36) NOT NULL,
  product_type VARCHAR(255) DEFAULT NULL,
  purchase_date DATETIME DEFAULT NULL,
  item_status ENUM('delivered','damaged','returned','unknown') DEFAULT 'unknown',
  amount_paid DECIMAL(10,2) DEFAULT NULL,
  prior_refund_total DECIMAL(10,2) DEFAULT '0.00',
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (order_id),
  KEY idx_orders_customer (customer_id),
  CONSTRAINT fk_orders_customer
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE tickets (
  ticket_id VARCHAR(36) NOT NULL,
  customer_id VARCHAR(36) NOT NULL,
  raw_text LONGTEXT NOT NULL,
  sanitized_text LONGTEXT,
  refund_reason VARCHAR(255) DEFAULT NULL,
  requested_amount DECIMAL(10,2) DEFAULT NULL,
  currency VARCHAR(3) DEFAULT 'USD',
  status VARCHAR(32) NOT NULL DEFAULT 'new',
  injection_flag TINYINT(1) DEFAULT '0',
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ticket_id),
  KEY idx_tickets_customer (customer_id),
  KEY idx_tickets_created (created_at),
  CONSTRAINT fk_tickets_customer
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE workflow_runs (
  trace_id VARCHAR(36) NOT NULL,
  ticket_id VARCHAR(36) NOT NULL,
  status ENUM('running','waiting_user','paused_governance','pending_human','completed','failed') DEFAULT 'running',
  current_agent VARCHAR(255) DEFAULT NULL,
  policy_version VARCHAR(50) DEFAULT NULL,
  started_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  completed_at TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (trace_id),
  KEY idx_workflow_ticket (ticket_id),
  KEY idx_workflow_status (status),
  KEY idx_workflow_started (started_at),
  CONSTRAINT fk_workflow_ticket
    FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE agent_handoffs (
  handoff_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  ticket_id VARCHAR(36) NOT NULL,
  from_agent VARCHAR(255) NOT NULL,
  to_agent VARCHAR(255) NOT NULL,
  input_json LONGTEXT NOT NULL,
  output_json LONGTEXT NOT NULL,
  input_tokens INT UNSIGNED DEFAULT NULL,
  output_tokens INT UNSIGNED DEFAULT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (handoff_id),
  KEY idx_handoffs_trace (trace_id),
  KEY idx_handoffs_agents (from_agent, to_agent),
  KEY idx_handoffs_created (created_at),
  KEY idx_handoffs_ticket (ticket_id),
  CONSTRAINT fk_handoffs_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id),
  CONSTRAINT fk_handoffs_ticket
    FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE audit_log (
  log_id BIGINT NOT NULL AUTO_INCREMENT,
  trace_id VARCHAR(36) DEFAULT NULL,
  event_type VARCHAR(50) NOT NULL COMMENT 'handoff, governance_block, refund_issued, human_approval, etc.',
  agent VARCHAR(255) DEFAULT NULL,
  payload_json LONGTEXT,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (log_id),
  KEY idx_audit_trace (trace_id),
  KEY idx_audit_event_type (event_type),
  KEY idx_audit_created (created_at),
  KEY idx_audit_agent (agent),
  CONSTRAINT fk_audit_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE governance_events (
  event_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  agent VARCHAR(255) NOT NULL,
  owasp_category VARCHAR(10) NOT NULL COMMENT 'ASI01-ASI10',
  trigger_score DECIMAL(5,3) DEFAULT NULL,
  interceptor_action ENUM('allow','quarantine','block') NOT NULL,
  flags_json LONGTEXT,
  offending_content LONGTEXT,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (event_id),
  KEY idx_governance_trace (trace_id),
  KEY idx_governance_owasp (owasp_category),
  KEY idx_governance_action (interceptor_action),
  KEY idx_governance_created (created_at),
  CONSTRAINT fk_governance_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE policy_review_events (
  policy_review_event_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  policy_version VARCHAR(50) NOT NULL,
  review_type VARCHAR(50) NOT NULL,
  policy_ids_json LONGTEXT NOT NULL,
  evidence_json LONGTEXT NOT NULL,
  detail LONGTEXT NOT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (policy_review_event_id),
  KEY idx_policy_review_trace (trace_id),
  KEY idx_policy_review_type (review_type),
  CONSTRAINT fk_policy_review_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE human_approvals (
  approval_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  triggering_event_id VARCHAR(36) NOT NULL,
  triggering_event_type ENUM('governance','policy_review') NOT NULL,
  reason VARCHAR(255) NOT NULL,
  amount_requested DECIMAL(10,2) DEFAULT NULL,
  resolved_amount DECIMAL(10,2) DEFAULT NULL,
  status ENUM('pending','approved','rejected') DEFAULT 'pending',
  decision ENUM('approve','partial_refund','deny','request_info') DEFAULT NULL,
  approved_next_agent VARCHAR(255) DEFAULT NULL,
  rejected_next_agent VARCHAR(255) DEFAULT NULL,
  reviewer VARCHAR(255) DEFAULT NULL,
  notes LONGTEXT,
  resolved_at TIMESTAMP NULL DEFAULT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (approval_id),
  KEY idx_approvals_trace (trace_id),
  KEY idx_approvals_status (status),
  KEY idx_approvals_created (created_at),
  KEY idx_approval_trigger (triggering_event_type, triggering_event_id),
  CONSTRAINT fk_approvals_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE refund_transactions (
  transaction_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  approval_id VARCHAR(36) DEFAULT NULL,
  amount DECIMAL(10,2) NOT NULL,
  currency VARCHAR(3) DEFAULT 'USD',
  status ENUM('pending','issued','failed','blocked') DEFAULT 'pending',
  external_ref VARCHAR(255) DEFAULT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (transaction_id),
  KEY idx_refunds_approval (approval_id),
  KEY idx_refunds_trace (trace_id),
  KEY idx_refunds_status (status),
  KEY idx_refunds_created (created_at),
  CONSTRAINT fk_refunds_workflow
    FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id),
  CONSTRAINT fk_refunds_approval
    FOREIGN KEY (approval_id) REFERENCES human_approvals (approval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
