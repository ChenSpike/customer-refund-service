-- AI Governance Capstone - Database Schema
-- MySQL database for refund workflow governance tracking

CREATE DATABASE IF NOT EXISTS main_db;
USE main_db;

-- Customers: Who is asking for a refund
CREATE TABLE customers (
  customer_id VARCHAR(36) PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE,
  full_name VARCHAR(255) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Tickets: Inbound refund requests
CREATE TABLE tickets (
  ticket_id VARCHAR(36) PRIMARY KEY,
  customer_id VARCHAR(36) NOT NULL,
  raw_text LONGTEXT NOT NULL,
  sanitized_text LONGTEXT,
  refund_reason VARCHAR(255),
  requested_amount DECIMAL(10, 2),
  currency VARCHAR(3) DEFAULT 'USD',
  status VARCHAR(50) DEFAULT 'new',
  injection_flag BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
  INDEX idx_customer (customer_id),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Orders: Mock order database for lookups
CREATE TABLE orders (
  order_id VARCHAR(36) PRIMARY KEY,
  customer_id VARCHAR(36) NOT NULL,
  product_type VARCHAR(255),
  purchase_date DATETIME,
  item_status ENUM('delivered', 'damaged', 'returned', 'unknown') DEFAULT 'unknown',
  amount_paid DECIMAL(10, 2),
  prior_refund_total DECIMAL(10, 2) DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
  INDEX idx_customer (customer_id),
  INDEX idx_order_id (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Workflow runs: One row per LangGraph run (trace_id)
CREATE TABLE workflow_runs (
  trace_id VARCHAR(36) PRIMARY KEY,
  ticket_id VARCHAR(36) NOT NULL,
  status ENUM('running', 'waiting_user', 'paused_governance', 'pending_human', 'completed', 'failed') DEFAULT 'running',
  current_agent VARCHAR(255),
  policy_version VARCHAR(50),
  started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  completed_at TIMESTAMP NULL,
  FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
  INDEX idx_ticket (ticket_id),
  INDEX idx_status (status),
  INDEX idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent handoffs: JSON in/out at each agent boundary
CREATE TABLE agent_handoffs (
  handoff_id VARCHAR(36) PRIMARY KEY,
  trace_id VARCHAR(36) NOT NULL,
  from_agent VARCHAR(255) NOT NULL,
  to_agent VARCHAR(255) NOT NULL,
  input_json LONGTEXT NOT NULL,
  output_json LONGTEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (trace_id) REFERENCES workflow_runs(trace_id),
  INDEX idx_trace (trace_id),
  INDEX idx_agents (from_agent, to_agent),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Governance events: Quarantine holds, OWASP flags, scores
CREATE TABLE governance_events (
  event_id VARCHAR(36) PRIMARY KEY,
  trace_id VARCHAR(36) NOT NULL,
  agent VARCHAR(255) NOT NULL,
  owasp_category VARCHAR(10) NOT NULL COMMENT 'ASI01-ASI10',
  trigger_score DECIMAL(5, 3),
  interceptor_action ENUM('allow', 'quarantine', 'block') NOT NULL,
  flags_json LONGTEXT,
  offending_content LONGTEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (trace_id) REFERENCES workflow_runs(trace_id),
  INDEX idx_trace (trace_id),
  INDEX idx_owasp (owasp_category),
  INDEX idx_action (interceptor_action),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Human-in-the-loop queue: Pause & wait decisions
CREATE TABLE human_approvals (
  approval_id VARCHAR(36) PRIMARY KEY,
  trace_id VARCHAR(36) NOT NULL,
  triggering_event_id VARCHAR(36) NULL COMMENT 'Governance event that escalated this approval, if any',
  reason VARCHAR(255) NOT NULL,
  amount_requested DECIMAL(10, 2),
  status ENUM('pending', 'approved', 'rejected') DEFAULT 'pending',
  decision ENUM('approve', 'partial_refund', 'deny', 'request_info') NULL COMMENT 'Reviewer''s actual choice; status derives from this (approve/partial_refund -> approved, deny/request_info -> rejected)',
  resolved_amount DECIMAL(10, 2) NULL COMMENT 'Reviewer-set refund amount for approve/partial_refund; falls back to amount_requested when NULL',
  reviewer VARCHAR(255),
  notes LONGTEXT,
  resolved_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (trace_id) REFERENCES workflow_runs(trace_id),
  FOREIGN KEY (triggering_event_id) REFERENCES governance_events(event_id),
  INDEX idx_trace (trace_id),
  INDEX idx_status (status),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Audit log: Append-only compliance trail
CREATE TABLE audit_log (
  log_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  trace_id VARCHAR(36),
  event_type VARCHAR(50) NOT NULL COMMENT 'handoff, governance_block, refund_issued, human_approval, etc.',
  agent VARCHAR(255),
  payload_json LONGTEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (trace_id) REFERENCES workflow_runs(trace_id),
  INDEX idx_trace (trace_id),
  INDEX idx_event_type (event_type),
  INDEX idx_created (created_at),
  INDEX idx_agent (agent)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Refund transactions: Actual money movement
CREATE TABLE refund_transactions (
  transaction_id VARCHAR(36) PRIMARY KEY,
  trace_id VARCHAR(36) NOT NULL,
  approval_id VARCHAR(36),
  amount DECIMAL(10, 2) NOT NULL,
  currency VARCHAR(3) DEFAULT 'USD',
  status ENUM('pending', 'issued', 'failed', 'blocked') DEFAULT 'pending',
  external_ref VARCHAR(255),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (trace_id) REFERENCES workflow_runs(trace_id),
  FOREIGN KEY (approval_id) REFERENCES human_approvals(approval_id),
  INDEX idx_trace (trace_id),
  INDEX idx_status (status),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
