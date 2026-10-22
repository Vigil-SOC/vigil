--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14
-- Dumped by pg_dump version 17.9 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: SCHEMA "public"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA "public" IS 'standard public schema';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "pg_trgm" WITH SCHEMA "public";


--
-- Name: EXTENSION "pg_trgm"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "pg_trgm" IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: uuid-ossp; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "public";


--
-- Name: EXTENSION "uuid-ossp"; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION "uuid-ossp" IS 'generate universally unique identifiers (UUIDs)';


--
-- Name: agent_event_hash("text", "jsonb"); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."agent_event_hash"("prev_hash" "text", "payload" "jsonb") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE
    SET "search_path" TO 'public'
    AS $$
  SELECT encode(
    sha256(convert_to(coalesce(prev_hash, '') || CAST(payload AS text), 'UTF8')),
    'hex'
  );
$$;


--
-- Name: agent_events_assign_hashes(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."agent_events_assign_hashes"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'public'
    AS $$
BEGIN
  SELECT event_hash INTO NEW.prev_hash
    FROM agent_events
    WHERE run_id = NEW.run_id AND seq < NEW.seq
    ORDER BY seq DESC
    LIMIT 1;
  NEW.prev_hash := coalesce(NEW.prev_hash, '');
  NEW.event_hash := agent_event_hash(NEW.prev_hash, NEW.payload);
  RETURN NEW;
END;
$$;


--
-- Name: update_ai_model_configs_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_ai_model_configs_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_custom_agents_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_custom_agents_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_integration_configs_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_integration_configs_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_llm_provider_configs_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_llm_provider_configs_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_system_config_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_system_config_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_updated_at_column"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_user_preferences_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION "public"."update_user_preferences_timestamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: agent_directives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."agent_directives" (
    "id" bigint NOT NULL,
    "run_id" "uuid" NOT NULL,
    "directive_id" "text" NOT NULL,
    "kind" "text" NOT NULL,
    "actor" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "payload" "jsonb" NOT NULL
);


--
-- Name: TABLE "agent_directives"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."agent_directives" IS 'Pending operator directives. Written by any process; drained onto agent_events by the run holding the ledger, which is what makes a directive take effect.';


--
-- Name: COLUMN "agent_directives"."directive_id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_directives"."directive_id" IS 'The directive''s identity on the ledger. Unique so a retried enqueue cannot queue the same directive twice, and so the drain can tell what it has already journaled.';


--
-- Name: COLUMN "agent_directives"."kind"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_directives"."kind" IS 'Directive kind, validated in TypeScript against a closed union rather than constrained here, as agent_events.kind is.';


--
-- Name: COLUMN "agent_directives"."payload"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_directives"."payload" IS 'The whole directive. The columns above are the envelope every workflow shares; everything a workflow owns its own vocabulary for stays in here, unread by the harness.';


--
-- Name: agent_directives_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."agent_directives_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_directives_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."agent_directives_id_seq" OWNED BY "public"."agent_directives"."id";


--
-- Name: agent_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."agent_events" (
    "run_id" "uuid" NOT NULL,
    "seq" integer NOT NULL,
    "ts" timestamp with time zone DEFAULT "now"() NOT NULL,
    "run_kind" "text" NOT NULL,
    "kind" "text" NOT NULL,
    "payload" "jsonb" NOT NULL,
    "snapshot" "jsonb",
    "schema_version" integer NOT NULL,
    "prev_hash" "text" NOT NULL,
    "event_hash" "text" NOT NULL
);


--
-- Name: TABLE "agent_events"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."agent_events" IS 'Append-only ledger owned solely by the agent layer; vigil_app may SELECT and INSERT only. The projection is folded from these rows and never stored.';


--
-- Name: COLUMN "agent_events"."kind"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_events"."kind" IS 'Event kind, validated in TypeScript against a closed union and scoped by run_kind.';


--
-- Name: COLUMN "agent_events"."snapshot"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_events"."snapshot" IS 'The digest presented to the lead, selected only by replay and never by the fold.';


--
-- Name: COLUMN "agent_events"."prev_hash"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_events"."prev_hash" IS 'event_hash of the previous seq in this run, or empty at the first row.';


--
-- Name: COLUMN "agent_events"."event_hash"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_events"."event_hash" IS 'sha256(prev_hash || CAST(payload AS text)), assigned on INSERT and never by the caller.';


--
-- Name: agent_run_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."agent_run_leases" (
    "run_id" "uuid" NOT NULL,
    "run_kind" "text" NOT NULL,
    "owner" "text",
    "claim_until" timestamp with time zone NOT NULL
);


--
-- Name: TABLE "agent_run_leases"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."agent_run_leases" IS 'One row per in-flight run. The row exists until the run journals its terminal; claim_until says nobody may touch it before then.';


--
-- Name: COLUMN "agent_run_leases"."owner"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_run_leases"."owner" IS 'Which worker is driving the run, or NULL when nobody is -- reserved for the queue, parked on an answer, or never started. Whether it is null decides who may claim; which worker it names is only for the console to report.';


--
-- Name: COLUMN "agent_run_leases"."claim_until"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."agent_run_leases"."claim_until" IS 'Written and compared with the database clock only, so a worker whose clock runs fast cannot hold an unkillable lease.';


--
-- Name: ai_decision_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."ai_decision_logs" (
    "id" integer NOT NULL,
    "decision_id" character varying(50) NOT NULL,
    "agent_id" character varying(50) NOT NULL,
    "workflow_id" character varying(50),
    "finding_id" character varying(50),
    "case_id" character varying(50),
    "decision_type" character varying(50) NOT NULL,
    "confidence_score" double precision NOT NULL,
    "reasoning" "text" NOT NULL,
    "recommended_action" "text" NOT NULL,
    "decision_metadata" "jsonb",
    "human_reviewer" character varying(100),
    "human_decision" character varying(50),
    "feedback_comment" "text",
    "accuracy_grade" double precision,
    "reasoning_grade" double precision,
    "action_appropriateness" double precision,
    "actual_outcome" character varying(50),
    "time_saved_minutes" integer,
    "timestamp" timestamp without time zone DEFAULT "now"() NOT NULL,
    "feedback_timestamp" timestamp without time zone
);


--
-- Name: COLUMN "ai_decision_logs"."agent_id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."ai_decision_logs"."agent_id" IS 'Action id from core/agents/builtins.py (e.g. investigation, correlation), or orchestration for the autonomous loop''s own decisions';


--
-- Name: ai_decision_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."ai_decision_logs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_decision_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."ai_decision_logs_id_seq" OWNED BY "public"."ai_decision_logs"."id";


--
-- Name: ai_model_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."ai_model_configs" (
    "component" character varying(64) NOT NULL,
    "provider_id" character varying(64) NOT NULL,
    "model_id" character varying(200) NOT NULL,
    "settings" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "updated_by" character varying(64),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: TABLE "ai_model_configs"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."ai_model_configs" IS 'Per-component AI model assignments (GH #89)';


--
-- Name: COLUMN "ai_model_configs"."component"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."ai_model_configs"."component" IS 'chat_default | triage | investigation | summarization | reporting';


--
-- Name: COLUMN "ai_model_configs"."settings"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."ai_model_configs"."settings" IS 'Component-specific overrides (max_tokens, thinking_budget, temperature)';


--
-- Name: approval_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."approval_actions" (
    "action_id" character varying(80) NOT NULL,
    "action_type" character varying(40) NOT NULL,
    "title" "text" NOT NULL,
    "description" "text" NOT NULL,
    "target" "text" NOT NULL,
    "confidence" numeric(4,3) DEFAULT 0 NOT NULL,
    "reason" "text" NOT NULL,
    "evidence" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "created_by" character varying(100) NOT NULL,
    "requires_approval" boolean DEFAULT true NOT NULL,
    "status" character varying(16) NOT NULL,
    "approved_at" timestamp without time zone,
    "approved_by" character varying(100),
    "executed_at" timestamp without time zone,
    "execution_result" "jsonb",
    "rejection_reason" "text",
    "parameters" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "workflow_run_id" character varying(80),
    "workflow_phase_id" "text",
    "reversibility" character varying(16) DEFAULT 'reversible'::character varying NOT NULL,
    "idempotency_key" "text",
    CONSTRAINT "approval_actions_reversibility_check" CHECK (("reversibility" IN ('reversible', 'irreversible'))),
    CONSTRAINT "approval_actions_status_check" CHECK (("status" IN ('pending', 'approved', 'rejected', 'executed', 'failed')))
);


--
-- Name: TABLE "approval_actions"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."approval_actions" IS 'Pending human-in-the-loop approvals. Workflow phase approvals link back via workflow_run_id + workflow_phase_id (#128).';


--
-- Name: COLUMN "approval_actions"."workflow_run_id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."approval_actions"."workflow_run_id" IS 'FK to workflow_runs — null for non-workflow approvals (e.g. daemon containment actions).';


--
-- Name: COLUMN "approval_actions"."workflow_phase_id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."approval_actions"."workflow_phase_id" IS 'The phase_id inside the paused workflow run this approval gates.';


--
-- Name: COLUMN "approval_actions"."reversibility"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."approval_actions"."reversibility" IS 'Whether the action can be undone. Irreversible rows always require approval, regardless of confidence.';


--
-- Name: COLUMN "approval_actions"."idempotency_key"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."approval_actions"."idempotency_key" IS 'Caller-supplied key; unique among non-failed rows so a repeated isolate of the same target is one row.';


--
-- Name: case_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_attachments" (
    "attachment_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "filename" character varying(255) NOT NULL,
    "file_path" character varying(500) NOT NULL,
    "file_type" character varying(100),
    "file_size" integer NOT NULL,
    "mime_type" character varying(100),
    "uploaded_by" character varying(100) NOT NULL,
    "description" "text",
    "tags" character varying[],
    "virus_scan_result" character varying(50),
    "scan_details" "jsonb",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_attachments_attachment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_attachments_attachment_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_attachments_attachment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_attachments_attachment_id_seq" OWNED BY "public"."case_attachments"."attachment_id";


--
-- Name: case_audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_audit_logs" (
    "audit_id" integer NOT NULL,
    "entity_type" character varying(50) NOT NULL,
    "entity_id" character varying(100) NOT NULL,
    "action" character varying(20) NOT NULL,
    "field_name" character varying(100),
    "old_value" "text",
    "new_value" "text",
    "change_summary" "text",
    "changed_by" character varying(100) NOT NULL,
    "user_agent" character varying(200),
    "ip_address" character varying(45),
    "timestamp" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_audit_logs_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_audit_logs_audit_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_audit_logs_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_audit_logs_audit_id_seq" OWNED BY "public"."case_audit_logs"."audit_id";


--
-- Name: case_closure_info; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_closure_info" (
    "case_id" character varying(50) NOT NULL,
    "closure_category" character varying(50) NOT NULL,
    "root_cause" "text",
    "contributing_factors" character varying[],
    "lessons_learned" "text",
    "recommendations" "text",
    "recurrence_prevention" "text",
    "false_positive_reason" "text",
    "executive_summary" "text",
    "closed_by" character varying(100) NOT NULL,
    "closed_by_kind" character varying(16) DEFAULT 'agent'::character varying NOT NULL,
    "closure_notes" "text",
    "closed_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "case_closure_info_closed_by_kind_check" CHECK (("closed_by_kind" IN ('analyst', 'agent')))
);


--
-- Name: COLUMN "case_closure_info"."closed_by_kind"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."case_closure_info"."closed_by_kind" IS 'Whether a person or an agent closed this case; read by episodic memory as Trust.';


--
-- Name: case_comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_comments" (
    "comment_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "parent_comment_id" integer,
    "author" character varying(100) NOT NULL,
    "content" "text" NOT NULL,
    "mentions" character varying[],
    "attachment_ids" integer[],
    "is_edited" boolean NOT NULL,
    "is_deleted" boolean NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_comments_comment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_comments_comment_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_comments_comment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_comments_comment_id_seq" OWNED BY "public"."case_comments"."comment_id";


--
-- Name: case_escalations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_escalations" (
    "escalation_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "escalated_from" character varying(100) NOT NULL,
    "escalated_to" character varying(100) NOT NULL,
    "reason" "text" NOT NULL,
    "urgency_level" character varying(20) NOT NULL,
    "status" character varying(20) NOT NULL,
    "escalated_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "acknowledged_at" timestamp without time zone,
    "resolved_at" timestamp without time zone,
    "resolution_notes" "text"
);


--
-- Name: case_escalations_escalation_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_escalations_escalation_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_escalations_escalation_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_escalations_escalation_id_seq" OWNED BY "public"."case_escalations"."escalation_id";


--
-- Name: case_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_evidence" (
    "evidence_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "evidence_type" character varying(50) NOT NULL,
    "name" character varying(200) NOT NULL,
    "description" "text",
    "file_path" character varying(500),
    "file_size" integer,
    "file_hash_md5" character varying(32),
    "file_hash_sha256" character varying(64),
    "source" character varying(200),
    "collected_by" character varying(100) NOT NULL,
    "collected_at" timestamp without time zone NOT NULL,
    "chain_of_custody" "jsonb" NOT NULL,
    "analysis_results" "jsonb",
    "tags" character varying[],
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_evidence_evidence_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_evidence_evidence_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_evidence_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_evidence_evidence_id_seq" OWNED BY "public"."case_evidence"."evidence_id";


--
-- Name: case_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_findings" (
    "case_id" character varying NOT NULL,
    "finding_id" character varying NOT NULL,
    "added_at" timestamp without time zone NOT NULL
);


--
-- Name: case_iocs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_iocs" (
    "ioc_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "ioc_type" character varying(50) NOT NULL,
    "value" character varying(500) NOT NULL,
    "threat_level" character varying(20),
    "confidence" double precision,
    "source" character varying(200),
    "first_seen" timestamp without time zone,
    "last_seen" timestamp without time zone,
    "enrichment_data" "jsonb",
    "reputation_score" double precision,
    "tags" character varying[],
    "context" "text",
    "is_active" boolean NOT NULL,
    "is_false_positive" boolean NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_iocs_ioc_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_iocs_ioc_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_iocs_ioc_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_iocs_ioc_id_seq" OWNED BY "public"."case_iocs"."ioc_id";


--
-- Name: case_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_metrics" (
    "case_id" character varying(50) NOT NULL,
    "time_to_detect" integer,
    "time_to_respond" integer,
    "time_to_contain" integer,
    "time_to_resolve" integer,
    "total_work_hours" double precision,
    "analyst_handoffs_count" integer NOT NULL,
    "sla_met" boolean,
    "response_sla_met" boolean,
    "resolution_sla_met" boolean,
    "comment_count" integer NOT NULL,
    "evidence_count" integer NOT NULL,
    "ioc_count" integer NOT NULL,
    "task_count" integer NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_notifications" (
    "notification_id" integer NOT NULL,
    "case_id" character varying(50),
    "user_id" character varying(100) NOT NULL,
    "notification_type" character varying(50) NOT NULL,
    "title" character varying(200) NOT NULL,
    "message" "text" NOT NULL,
    "delivery_channel" character varying(20) NOT NULL,
    "priority" character varying(20) NOT NULL,
    "is_read" boolean NOT NULL,
    "is_sent" boolean NOT NULL,
    "sent_at" timestamp without time zone,
    "read_at" timestamp without time zone,
    "notification_metadata" "jsonb",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_notifications_notification_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_notifications_notification_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_notifications_notification_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_notifications_notification_id_seq" OWNED BY "public"."case_notifications"."notification_id";


--
-- Name: case_relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_relationships" (
    "relationship_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "related_case_id" character varying(50) NOT NULL,
    "relationship_type" character varying(50) NOT NULL,
    "created_by" character varying(100) NOT NULL,
    "notes" "text",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_relationships_relationship_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_relationships_relationship_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_relationships_relationship_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_relationships_relationship_id_seq" OWNED BY "public"."case_relationships"."relationship_id";


--
-- Name: case_slas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_slas" (
    "sla_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "sla_policy_id" character varying(50) NOT NULL,
    "response_due" timestamp without time zone NOT NULL,
    "resolution_due" timestamp without time zone NOT NULL,
    "response_completed_at" timestamp without time zone,
    "response_sla_met" boolean,
    "resolution_completed_at" timestamp without time zone,
    "resolution_sla_met" boolean,
    "breached" boolean NOT NULL,
    "breach_time" timestamp without time zone,
    "breach_reason" "text",
    "is_paused" boolean NOT NULL,
    "paused_at" timestamp without time zone,
    "resumed_at" timestamp without time zone,
    "total_pause_duration" integer NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_slas_sla_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_slas_sla_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_slas_sla_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_slas_sla_id_seq" OWNED BY "public"."case_slas"."sla_id";


--
-- Name: case_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_tasks" (
    "task_id" integer NOT NULL,
    "case_id" character varying(50) NOT NULL,
    "parent_task_id" integer,
    "title" character varying(200) NOT NULL,
    "description" "text",
    "assignee" character varying(100),
    "status" character varying(20) NOT NULL,
    "priority" character varying(20) NOT NULL,
    "due_date" timestamp without time zone,
    "completed_at" timestamp without time zone,
    "estimated_hours" double precision,
    "actual_hours" double precision,
    "checklist_items" "jsonb",
    "task_order" integer NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_tasks_task_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."case_tasks_task_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: case_tasks_task_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."case_tasks_task_id_seq" OWNED BY "public"."case_tasks"."task_id";


--
-- Name: case_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_templates" (
    "template_id" character varying(50) NOT NULL,
    "name" character varying(200) NOT NULL,
    "description" "text",
    "template_type" character varying(50) NOT NULL,
    "default_priority" character varying(20) NOT NULL,
    "default_status" character varying(20) NOT NULL,
    "default_sla_policy_id" character varying(50),
    "task_templates" "jsonb" NOT NULL,
    "playbook_steps" "jsonb",
    "applicable_mitre_techniques" character varying[],
    "tags" character varying[],
    "is_active" boolean NOT NULL,
    "usage_count" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: case_watchers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."case_watchers" (
    "case_id" character varying(50) NOT NULL,
    "user_id" character varying(100) NOT NULL,
    "notification_preferences" "jsonb",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."cases" (
    "case_id" character varying(50) NOT NULL,
    "title" character varying(200) NOT NULL,
    "description" "text",
    "status" character varying(20) DEFAULT 'new'::character varying NOT NULL,
    "priority" character varying(20) DEFAULT 'medium'::character varying NOT NULL,
    "assignee" character varying(100),
    "tags" character varying[],
    "notes" "jsonb",
    "timeline" "jsonb" NOT NULL,
    "activities" "jsonb",
    "resolution_steps" "jsonb",
    "mitre_techniques" character varying[],
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."chat_messages" (
    "id" integer NOT NULL,
    "conversation_id" character varying(120) NOT NULL,
    "seq" integer NOT NULL,
    "role" character varying(20) NOT NULL,
    "content" "text" NOT NULL,
    "thinking" "text",
    "tool_calls" "jsonb" NOT NULL,
    "complete" boolean DEFAULT true NOT NULL,
    "model" character varying(80),
    "input_tokens" integer DEFAULT 0 NOT NULL,
    "output_tokens" integer DEFAULT 0 NOT NULL,
    "cost_usd" numeric(10,6) DEFAULT '0'::numeric NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: chat_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."chat_messages_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."chat_messages_id_seq" OWNED BY "public"."chat_messages"."id";


--
-- Name: config_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."config_audit_log" (
    "id" integer NOT NULL,
    "config_type" character varying(50) NOT NULL,
    "config_key" character varying(200) NOT NULL,
    "action" character varying(20) NOT NULL,
    "old_value" "jsonb",
    "new_value" "jsonb",
    "changed_by" character varying(100) NOT NULL,
    "change_reason" "text",
    "timestamp" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: TABLE "config_audit_log"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."config_audit_log" IS 'Audit trail of all configuration changes';


--
-- Name: COLUMN "config_audit_log"."action"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."config_audit_log"."action" IS 'Type of change: create, update, or delete';


--
-- Name: config_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."config_audit_log_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: config_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."config_audit_log_id_seq" OWNED BY "public"."config_audit_log"."id";


--
-- Name: conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."conversations" (
    "id" character varying(120) NOT NULL,
    "user_id" character varying(50),
    "title" character varying(200),
    "case_id" character varying(50),
    "page_context" character varying(120),
    "agent_id" character varying(60),
    "model" character varying(80),
    "archived" boolean DEFAULT false NOT NULL,
    "message_count" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "last_message_at" timestamp without time zone
);


--
-- Name: custom_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."custom_agents" (
    "id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "icon" "text",
    "color" "text",
    "specialization" "text",
    "role" "text" NOT NULL,
    "extra_principles" "text" DEFAULT ''::"text" NOT NULL,
    "methodology" "text" DEFAULT ''::"text" NOT NULL,
    "system_prompt_override" "text",
    "recommended_tools" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "max_tokens" integer DEFAULT 4096 NOT NULL,
    "enable_thinking" boolean DEFAULT false NOT NULL,
    "model" "text",
    "forked_from" "text",
    "created_by" character varying(100),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "component_category" character varying(32) DEFAULT 'investigation'::character varying NOT NULL,
    "fallback_model" "text"
);


--
-- Name: TABLE "custom_agents"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."custom_agents" IS 'User-defined SOC agents created via the Agent Builder UI';


--
-- Name: COLUMN "custom_agents"."id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."id" IS 'Agent identifier, format "custom-<slug>"';


--
-- Name: COLUMN "custom_agents"."role"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."role" IS 'Injected into BASE_PROMPT {role} placeholder';


--
-- Name: COLUMN "custom_agents"."extra_principles"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."extra_principles" IS 'Injected into BASE_PROMPT {extra_principles} placeholder';


--
-- Name: COLUMN "custom_agents"."methodology"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."methodology" IS 'Injected into BASE_PROMPT {methodology} placeholder';


--
-- Name: COLUMN "custom_agents"."system_prompt_override"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."system_prompt_override" IS 'When set, bypasses BASE_PROMPT and uses this text verbatim';


--
-- Name: COLUMN "custom_agents"."recommended_tools"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."recommended_tools" IS 'Array of MCP tool names (full {server}_{tool} format)';


--
-- Name: COLUMN "custom_agents"."model"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."model" IS 'Reserved for per-agent model override (issue #89); null = use caller default';


--
-- Name: COLUMN "custom_agents"."component_category"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."component_category" IS 'triage | investigation | reporting — maps to ai_model_configs components';


--
-- Name: COLUMN "custom_agents"."fallback_model"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."custom_agents"."fallback_model" IS 'Optional second model id for chat. Null = unset. Tried after model on the assignment provider.';


--
-- Name: custom_workflows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."custom_workflows" (
    "workflow_id" character varying(100) NOT NULL,
    "name" character varying(200) NOT NULL,
    "description" "text" NOT NULL,
    "use_case" "text",
    "trigger_examples" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "phases" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "graph_layout" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "created_by" character varying(100),
    "version" integer DEFAULT 1 NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: integration_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."integration_configs" (
    "integration_id" character varying(100) NOT NULL,
    "enabled" boolean DEFAULT false NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "integration_name" character varying(200),
    "integration_type" character varying(50),
    "description" "text",
    "last_test_at" timestamp without time zone,
    "last_test_success" boolean,
    "last_error" "text",
    "updated_by" character varying(100),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: TABLE "integration_configs"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."integration_configs" IS 'Integration configuration (non-sensitive settings only)';


--
-- Name: COLUMN "integration_configs"."config"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."integration_configs"."config" IS 'Integration-specific configuration as JSONB';


--
-- Name: COLUMN "integration_configs"."last_test_at"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."integration_configs"."last_test_at" IS 'Last time integration was tested';


--
-- Name: enabled_integrations; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."enabled_integrations" AS
 SELECT "integration_id",
    "integration_name",
    "integration_type",
    "config",
    "last_test_at",
    "last_test_success",
    "updated_at"
   FROM "public"."integration_configs"
  WHERE ("enabled" = true)
  ORDER BY "integration_name";


--
-- Name: VIEW "enabled_integrations"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW "public"."enabled_integrations" IS 'Quick view of all enabled integrations';


--
-- Name: episodic_distil_failures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_distil_failures" (
    "investigation_kind" "text" NOT NULL,
    "failure_key" "text" NOT NULL,
    "origin_seq" integer,
    "reason" "text" NOT NULL,
    "attempts" integer NOT NULL,
    "last_error" "text" NOT NULL,
    "first_failed_at" timestamp with time zone NOT NULL,
    "last_failed_at" timestamp with time zone NOT NULL,
    "next_attempt_at" timestamp with time zone NOT NULL,
    "distil_version" integer NOT NULL,
    CONSTRAINT "episodic_distil_failures_attempts_check" CHECK (("attempts" > 0)),
    CONSTRAINT "episodic_distil_failures_investigation_kind_check" CHECK (("investigation_kind" = ANY (ARRAY['hunt'::"text", 'case'::"text", 'analyst'::"text"]))),
    CONSTRAINT "episodic_distil_failures_reason_check" CHECK (("reason" = ANY (ARRAY['refused'::"text", 'unreadable'::"text", 'failed'::"text"]))),
    CONSTRAINT "episodic_distil_failures_seq_matches_kind" CHECK ((("investigation_kind" = 'hunt'::"text") = ("origin_seq" IS NOT NULL))),
    CONSTRAINT "episodic_distil_failures_window" CHECK (("first_failed_at" <= "last_failed_at"))
);


--
-- Name: TABLE "episodic_distil_failures"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_distil_failures" IS 'One row per subject a Distil could not write, with why, how often, and when it may be tried again.';


--
-- Name: episodic_distil_markers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_distil_markers" (
    "investigation_kind" "text" NOT NULL,
    "investigation_id" "text" NOT NULL,
    "origin_run_id" "uuid",
    "origin_seq" integer,
    "origin_run_ids" "uuid"[] DEFAULT ARRAY[]::"uuid"[] NOT NULL,
    "distil_version" integer NOT NULL,
    "sightings_written" integer NOT NULL,
    "verdicts_written" integer NOT NULL,
    "gaps_written" integer NOT NULL,
    "concluded_at" timestamp with time zone NOT NULL,
    "distilled_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "episodic_distil_markers_gaps_written_check" CHECK (("gaps_written" >= 0)),
    CONSTRAINT "episodic_distil_markers_investigation_kind_check" CHECK (("investigation_kind" = ANY (ARRAY['hunt'::"text", 'case'::"text", 'analyst'::"text"]))),
    CONSTRAINT "episodic_distil_markers_origin_matches_kind" CHECK (((("investigation_kind" = 'hunt'::"text") = ("origin_run_id" IS NOT NULL)) AND (("origin_run_id" IS NULL) = ("origin_seq" IS NULL)))),
    CONSTRAINT "episodic_distil_markers_sightings_written_check" CHECK (("sightings_written" >= 0)),
    CONSTRAINT "episodic_distil_markers_verdicts_written_check" CHECK (("verdicts_written" >= 0))
);


--
-- Name: TABLE "episodic_distil_markers"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_distil_markers" IS 'One row per distilled investigation, written in the same transaction as its rows.';


--
-- Name: episodic_gaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_gaps" (
    "id" bigint NOT NULL,
    "investigation_kind" "text" NOT NULL,
    "investigation_id" "text" NOT NULL,
    "hypothesis_id" "text" NOT NULL,
    "statement" "text" NOT NULL,
    "disposition" "text" NOT NULL,
    "reason" "text" NOT NULL,
    "subject_entities" "text"[] DEFAULT ARRAY[]::"text"[] NOT NULL,
    "concluded_at" timestamp with time zone NOT NULL,
    CONSTRAINT "episodic_gaps_disposition_check" CHECK (("disposition" = ANY (ARRAY['deprioritised'::"text", 'no_evidence_gathered'::"text", 'budget_exhausted'::"text"]))),
    CONSTRAINT "episodic_gaps_investigation_kind_check" CHECK (("investigation_kind" = ANY (ARRAY['hunt'::"text", 'case'::"text", 'analyst'::"text"])))
);


--
-- Name: TABLE "episodic_gaps"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_gaps" IS 'A question an investigation left unanswered, with why nothing was gathered for it.';


--
-- Name: episodic_gaps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."episodic_gaps_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: episodic_gaps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."episodic_gaps_id_seq" OWNED BY "public"."episodic_gaps"."id";


--
-- Name: episodic_read_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_read_log" (
    "id" bigint NOT NULL,
    "ts" timestamp with time zone DEFAULT "now"() NOT NULL,
    "caller_kind" "text" DEFAULT 'unknown'::"text" NOT NULL,
    "caller_id" "text" DEFAULT 'unknown'::"text" NOT NULL,
    "keys" "text"[] NOT NULL,
    "as_of" timestamp with time zone NOT NULL,
    "row_counts" "jsonb" NOT NULL,
    "dropped" "jsonb" NOT NULL,
    "ranking" "jsonb" NOT NULL
);


--
-- Name: TABLE "episodic_read_log"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_read_log" IS 'One row per read of episodic memory, for audit rather than replay. Retention is the daemon''s cleanup sweep, which deletes rows older than scheduler.cleanup_retention_days -- 90 days unless a deployment says otherwise.';


--
-- Name: COLUMN "episodic_read_log"."keys"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."episodic_read_log"."keys" IS 'The Entity Keys as queried -- normalised -- rather than as the caller spelled them.';


--
-- Name: COLUMN "episodic_read_log"."as_of"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."episodic_read_log"."as_of" IS 'The freshness filter the read ran with, which is not the time the read happened.';


--
-- Name: episodic_read_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."episodic_read_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: episodic_read_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."episodic_read_log_id_seq" OWNED BY "public"."episodic_read_log"."id";


--
-- Name: episodic_sightings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_sightings" (
    "id" bigint NOT NULL,
    "entity_key" "text" NOT NULL,
    "investigation_kind" "text" NOT NULL,
    "investigation_id" "text" NOT NULL,
    "source_system" "text" NOT NULL,
    "hit_count" integer NOT NULL,
    "attacker_influenceable" boolean NOT NULL,
    "first_seen" timestamp with time zone NOT NULL,
    "last_seen" timestamp with time zone NOT NULL,
    "concluded_at" timestamp with time zone NOT NULL,
    CONSTRAINT "episodic_sightings_hit_count_check" CHECK (("hit_count" > 0)),
    CONSTRAINT "episodic_sightings_investigation_kind_check" CHECK (("investigation_kind" = ANY (ARRAY['hunt'::"text", 'case'::"text", 'analyst'::"text"]))),
    CONSTRAINT "episodic_sightings_window" CHECK (("first_seen" <= "last_seen"))
);


--
-- Name: TABLE "episodic_sightings"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_sightings" IS 'What an investigation observed: one row per entity, investigation and source, never per evidence record.';


--
-- Name: episodic_sightings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."episodic_sightings_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: episodic_sightings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."episodic_sightings_id_seq" OWNED BY "public"."episodic_sightings"."id";


--
-- Name: episodic_verdict_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_verdict_sources" (
    "id" bigint NOT NULL,
    "verdict_id" bigint NOT NULL,
    "source_system" "text" NOT NULL,
    "stance" "text" NOT NULL,
    "source_tier" "text" NOT NULL,
    CONSTRAINT "episodic_verdict_sources_source_tier_check" CHECK (("source_tier" = ANY (ARRAY['telemetry'::"text", 'feed'::"text", 'not_evidence'::"text"]))),
    CONSTRAINT "episodic_verdict_sources_stance_check" CHECK (("stance" = ANY (ARRAY['supports'::"text", 'weakens'::"text", 'neither'::"text"])))
);


--
-- Name: TABLE "episodic_verdict_sources"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_verdict_sources" IS 'Per-source Stance and stamped Source Tier for one Verdict; the tier is a fact of write time.';


--
-- Name: episodic_verdict_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."episodic_verdict_sources_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: episodic_verdict_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."episodic_verdict_sources_id_seq" OWNED BY "public"."episodic_verdict_sources"."id";


--
-- Name: episodic_verdicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."episodic_verdicts" (
    "id" bigint NOT NULL,
    "investigation_kind" "text" NOT NULL,
    "investigation_id" "text" NOT NULL,
    "hypothesis_id" "text" NOT NULL,
    "statement" "text" NOT NULL,
    "outcome" "text" NOT NULL,
    "rationale" "text" NOT NULL,
    "subject_entities" "text"[] DEFAULT ARRAY[]::"text"[] NOT NULL,
    "attacker_influenceable_only" boolean NOT NULL,
    "trust" "text" NOT NULL,
    "first_seen" timestamp with time zone NOT NULL,
    "last_seen" timestamp with time zone NOT NULL,
    "window_source" "text" NOT NULL,
    "concluded_at" timestamp with time zone NOT NULL,
    "techniques" "text"[] DEFAULT ARRAY[]::"text"[] NOT NULL,
    CONSTRAINT "episodic_verdicts_investigation_kind_check" CHECK (("investigation_kind" = ANY (ARRAY['hunt'::"text", 'case'::"text", 'analyst'::"text"]))),
    CONSTRAINT "episodic_verdicts_outcome_check" CHECK (("outcome" = ANY (ARRAY['proven'::"text", 'disproven'::"text", 'inconclusive'::"text", 'handed_off'::"text", 'false_positive'::"text"]))),
    CONSTRAINT "episodic_verdicts_trust_check" CHECK (("trust" = ANY (ARRAY['analyst'::"text", 'agent'::"text"]))),
    CONSTRAINT "episodic_verdicts_window" CHECK (("first_seen" <= "last_seen")),
    CONSTRAINT "episodic_verdicts_window_source_check" CHECK (("window_source" = ANY (ARRAY['observed'::"text", 'asserted'::"text"])))
);


--
-- Name: TABLE "episodic_verdicts"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."episodic_verdicts" IS 'One conclusion per Hypothesis, naming its subject entities and not its evidence''s.';


--
-- Name: COLUMN "episodic_verdicts"."techniques"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."episodic_verdicts"."techniques" IS 'Distinct ATT&CK ids the gathered evidence bearing on this Hypothesis cited; empty is known-to-be-none.';


--
-- Name: episodic_verdicts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."episodic_verdicts_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: episodic_verdicts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."episodic_verdicts_id_seq" OWNED BY "public"."episodic_verdicts"."id";


--
-- Name: federation_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."federation_sources" (
    "source_id" character varying(64) NOT NULL,
    "enabled" boolean DEFAULT false NOT NULL,
    "interval_seconds" integer DEFAULT 300 NOT NULL,
    "max_items" integer DEFAULT 100 NOT NULL,
    "min_severity" character varying(16),
    "cursor" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "last_poll_at" timestamp with time zone,
    "last_success_at" timestamp with time zone,
    "last_error" "text",
    "consecutive_errors" integer DEFAULT 0 NOT NULL,
    "dropped_total" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


--
-- Name: finding_mitre_predictions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."finding_mitre_predictions" (
    "finding_id" character varying(50) NOT NULL,
    "technique_id" "text" NOT NULL,
    "confidence" double precision NOT NULL
);


--
-- Name: findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."findings" (
    "finding_id" character varying(50) NOT NULL,
    "anomaly_score" double precision,
    "title" "text",
    "description" "text",
    "entity_context" "jsonb",
    "evidence_links" "jsonb",
    "timestamp" timestamp without time zone,
    "data_source" character varying(50) NOT NULL,
    "external_id" character varying(255),
    "cluster_id" character varying(50),
    "severity" character varying(20),
    "status" character varying(20) DEFAULT 'new'::character varying NOT NULL,
    "noise_marked_at" timestamp without time zone,
    "noise_marked_by" character varying(50),
    "ai_enrichment" "jsonb",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: intake_triggers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."intake_triggers" (
    "id" integer NOT NULL,
    "kind" character varying(20) NOT NULL,
    "state" character varying(20) NOT NULL,
    "reason" "text",
    "finding_id" character varying(50),
    "priority" character varying(20) NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "investigation_id" character varying(60),
    "case_id" character varying(50),
    "merged_into" character varying(60),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "decided_at" timestamp without time zone
);


--
-- Name: intake_triggers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."intake_triggers_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: intake_triggers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."intake_triggers_id_seq" OWNED BY "public"."intake_triggers"."id";


--
-- Name: investigation_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."investigation_logs" (
    "id" integer NOT NULL,
    "investigation_id" character varying(60) NOT NULL,
    "timestamp" timestamp without time zone DEFAULT "now"() NOT NULL,
    "event_type" character varying(30) NOT NULL,
    "details" "jsonb" NOT NULL,
    "tokens_used" integer NOT NULL
);


--
-- Name: investigation_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."investigation_logs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: investigation_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."investigation_logs_id_seq" OWNED BY "public"."investigation_logs"."id";


--
-- Name: investigations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."investigations" (
    "investigation_id" character varying(60) NOT NULL,
    "case_id" character varying(50),
    "workflow_id" character varying(50) NOT NULL,
    "trigger_type" character varying(30) NOT NULL,
    "trigger_ids" "jsonb" NOT NULL,
    "status" character varying(20) NOT NULL,
    "workdir" character varying(255) NOT NULL,
    "current_step" integer NOT NULL,
    "total_steps" integer NOT NULL,
    "iteration_count" integer NOT NULL,
    "max_iterations" integer NOT NULL,
    "input_tokens" integer NOT NULL,
    "output_tokens" integer NOT NULL,
    "cost_usd" double precision NOT NULL,
    "max_cost_usd" double precision NOT NULL,
    "priority" character varying(20) NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "started_at" timestamp without time zone,
    "completed_at" timestamp without time zone,
    "last_activity_at" timestamp without time zone,
    "max_runtime_seconds" integer NOT NULL,
    "summary" "text",
    "proposed_actions" "jsonb",
    "master_review_notes" "text",
    "error_count" integer NOT NULL,
    "last_error" "text",
    "current_activity" "text"
);


--
-- Name: ip_exclusions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."ip_exclusions" (
    "exclusion_id" character varying(50) NOT NULL,
    "ip" character varying(45) NOT NULL,
    "reason" "text" NOT NULL,
    "origin" character varying(20) DEFAULT 'ad_hoc'::character varying NOT NULL,
    "origin_ref" character varying(100),
    "created_by" character varying(100) NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "removed_at" timestamp without time zone,
    "removed_by" character varying(100),
    "removal_reason" "text",
    CONSTRAINT "ck_ip_exclusions_origin" CHECK (("origin" IN ('ad_hoc', 'finding', 'case', 'run'))),
    CONSTRAINT "ck_ip_exclusions_removal" CHECK ((("removed_at" IS NULL) = ("removed_by" IS NULL)))
);


--
-- Name: TABLE "ip_exclusions"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."ip_exclusions" IS 'Analyst IP exclusions: hidden from the findings queue and not used to seed hunts/investigations. Ingest ignores this table.';


--
-- Name: llm_interaction_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."llm_interaction_logs" (
    "id" integer NOT NULL,
    "interaction_id" character varying(60) NOT NULL,
    "session_id" character varying(120),
    "agent_id" character varying(60),
    "investigation_id" character varying(60),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "model" character varying(80) NOT NULL,
    "request_messages" "jsonb" NOT NULL,
    "system_prompt" "text",
    "thinking_enabled" boolean NOT NULL,
    "thinking_budget" integer,
    "thinking_content" "text",
    "response_content" "text",
    "tool_calls" "jsonb" NOT NULL,
    "tool_results" "jsonb" NOT NULL,
    "stop_reason" character varying(40),
    "input_tokens" integer NOT NULL,
    "output_tokens" integer NOT NULL,
    "cache_read_tokens" integer NOT NULL,
    "cache_creation_tokens" integer NOT NULL,
    "cost_usd" numeric(10,6),
    "input_cost_per_token" double precision,
    "output_cost_per_token" double precision,
    "cache_read_cost_per_token" double precision,
    "cache_write_cost_per_token" double precision,
    "rates_fetched_at" character varying(64),
    "duration_ms" integer NOT NULL,
    "error" "text",
    "virtual_key_id" character varying(64)
);


--
-- Name: llm_interaction_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."llm_interaction_logs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: llm_interaction_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."llm_interaction_logs_id_seq" OWNED BY "public"."llm_interaction_logs"."id";


--
-- Name: llm_provider_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."llm_provider_configs" (
    "provider_id" character varying(64) NOT NULL,
    "provider_type" character varying(32) NOT NULL,
    "name" character varying(200) NOT NULL,
    "base_url" character varying(500),
    "api_key_ref" character varying(200),
    "default_model" character varying(200) NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "is_default" boolean DEFAULT false NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "last_test_at" timestamp without time zone,
    "last_test_success" boolean,
    "last_error" "text",
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: TABLE "llm_provider_configs"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."llm_provider_configs" IS 'LLM provider configuration (non-sensitive; keys in secrets_manager)';


--
-- Name: COLUMN "llm_provider_configs"."provider_type"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."llm_provider_configs"."provider_type" IS 'anthropic | openai | ollama';


--
-- Name: COLUMN "llm_provider_configs"."api_key_ref"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."llm_provider_configs"."api_key_ref" IS 'Secret name in secrets_manager (never the key itself)';


--
-- Name: COLUMN "llm_provider_configs"."config"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."llm_provider_configs"."config" IS 'Provider-specific extras (e.g., openai organization, ollama pull policy)';


--
-- Name: mcp_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."mcp_credentials" (
    "credential_id" character varying(50) NOT NULL,
    "user_id" character varying(50) NOT NULL,
    "token_hash" character(64) NOT NULL,
    "label" character varying(200) NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "last_used_at" timestamp without time zone,
    "expires_at" timestamp without time zone,
    "revoked_at" timestamp without time zone
);


--
-- Name: recent_config_changes; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW "public"."recent_config_changes" AS
 SELECT "config_type",
    "config_key",
    "action",
    "changed_by",
    "timestamp",
    "change_reason"
   FROM "public"."config_audit_log"
  ORDER BY "timestamp" DESC
 LIMIT 100;


--
-- Name: VIEW "recent_config_changes"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW "public"."recent_config_changes" IS 'Most recent 100 configuration changes';


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."roles" (
    "role_id" character varying(50) NOT NULL,
    "name" character varying(100) NOT NULL,
    "description" "text" NOT NULL,
    "permissions" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "is_system_role" boolean DEFAULT false NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: shared_iocs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."shared_iocs" (
    "id" integer NOT NULL,
    "investigation_id" character varying(60) NOT NULL,
    "ioc_type" character varying(30) NOT NULL,
    "value" character varying(500) NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: shared_iocs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."shared_iocs_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shared_iocs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."shared_iocs_id_seq" OWNED BY "public"."shared_iocs"."id";


--
-- Name: sketch_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."sketch_mappings" (
    "id" integer NOT NULL,
    "case_id" character varying(50),
    "finding_id" character varying(50),
    "sketch_id" integer NOT NULL,
    "sketch_name" character varying(200) NOT NULL,
    "sketch_url" character varying(500),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: sketch_mappings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."sketch_mappings_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sketch_mappings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."sketch_mappings_id_seq" OWNED BY "public"."sketch_mappings"."id";


--
-- Name: sla_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."sla_policies" (
    "policy_id" character varying(50) NOT NULL,
    "name" character varying(100) NOT NULL,
    "description" "text",
    "priority_level" character varying(20) NOT NULL,
    "response_time_hours" double precision NOT NULL,
    "resolution_time_hours" double precision NOT NULL,
    "business_hours_only" boolean NOT NULL,
    "escalation_rules" "jsonb",
    "notification_thresholds" integer[],
    "is_active" boolean NOT NULL,
    "is_default" boolean NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: system_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."system_config" (
    "key" character varying(100) NOT NULL,
    "value" "jsonb" NOT NULL,
    "description" "text",
    "config_type" character varying(50) DEFAULT 'general'::character varying NOT NULL,
    "updated_by" character varying(100),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: TABLE "system_config"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."system_config" IS 'System-wide configuration settings';


--
-- Name: COLUMN "system_config"."key"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."system_config"."key" IS 'Configuration key (e.g., approval.force_manual_approval)';


--
-- Name: COLUMN "system_config"."value"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."system_config"."value" IS 'Configuration value as JSONB';


--
-- Name: COLUMN "system_config"."config_type"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."system_config"."config_type" IS 'Type/category of configuration (general, approval, theme, etc.)';


--
-- Name: threat_indicators; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."threat_indicators" (
    "id" bigint NOT NULL,
    "indicator_type" character varying(32) NOT NULL,
    "indicator_value" character varying(2048) NOT NULL,
    "source" character varying(64) NOT NULL,
    "collection_id" character varying(128),
    "confidence" numeric(5,2),
    "threat_level" character varying(16),
    "labels" "text"[] DEFAULT ARRAY[]::"text"[],
    "valid_from" timestamp without time zone,
    "valid_until" timestamp without time zone,
    "raw_stix" "jsonb",
    "first_seen" timestamp without time zone DEFAULT "now"() NOT NULL,
    "last_seen" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: threat_indicators_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."threat_indicators_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: threat_indicators_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."threat_indicators_id_seq" OWNED BY "public"."threat_indicators"."id";


--
-- Name: user_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."user_preferences" (
    "user_id" character varying(100) NOT NULL,
    "preferences" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "display_name" character varying(200),
    "email" character varying(200),
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "last_login" timestamp without time zone
);


--
-- Name: TABLE "user_preferences"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."user_preferences" IS 'Per-user preferences and settings';


--
-- Name: COLUMN "user_preferences"."preferences"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."user_preferences"."preferences" IS 'User preferences as JSONB (theme, notifications, etc.)';


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."users" (
    "user_id" character varying(50) NOT NULL,
    "username" character varying(100) NOT NULL,
    "email" character varying(200) NOT NULL,
    "password_hash" character varying(255) NOT NULL,
    "full_name" character varying(200) NOT NULL,
    "role_id" character varying(50) NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "is_verified" boolean DEFAULT false NOT NULL,
    "mfa_enabled" boolean DEFAULT false NOT NULL,
    "mfa_secret" character varying(255),
    "mfa_recovery_codes" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "last_login" timestamp without time zone,
    "login_count" integer DEFAULT 0 NOT NULL,
    "failed_login_count" integer DEFAULT 0 NOT NULL,
    "locked_until" timestamp without time zone,
    "password_history" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "password_changed_at" timestamp without time zone,
    "created_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"() NOT NULL
);


--
-- Name: workflow_run_phases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."workflow_run_phases" (
    "run_id" character varying(80) NOT NULL,
    "phase_id" "text" NOT NULL,
    "phase_order" integer NOT NULL,
    "agent_id" "text" NOT NULL,
    "status" character varying(16) NOT NULL,
    "started_at" timestamp without time zone,
    "finished_at" timestamp without time zone,
    "duration_ms" integer,
    "input_context" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "output" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "approval_state" character varying(16),
    "cost_usd" numeric(10,4) DEFAULT 0 NOT NULL,
    "error" "text",
    CONSTRAINT "workflow_run_phases_status_check" CHECK (("status" IN ('pending', 'running', 'completed', 'failed', 'skipped', 'pending_approval')))
);


--
-- Name: TABLE "workflow_run_phases"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."workflow_run_phases" IS 'Reserved for phase-by-phase workflow execution (#128). Rows are only written once phase-level execution lands; until then, runs surface at the workflow_runs level only.';


--
-- Name: workflow_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."workflow_runs" (
    "run_id" character varying(80) NOT NULL,
    "workflow_id" "text" NOT NULL,
    "workflow_version" integer,
    "workflow_source" character varying(16) DEFAULT 'file'::character varying NOT NULL,
    "workflow_name" "text" NOT NULL,
    "status" character varying(16) NOT NULL,
    "triggered_by" character varying(100),
    "trigger_context" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "started_at" timestamp without time zone DEFAULT "now"() NOT NULL,
    "finished_at" timestamp without time zone,
    "duration_ms" integer,
    "total_cost_usd" numeric(10,4) DEFAULT 0 NOT NULL,
    "result_summary" "text",
    "error" "text",
    "deleted_at" timestamp without time zone,
    "outcome" "text",
    "reason" "text",
    CONSTRAINT "workflow_runs_status_check" CHECK (("status" IN ('running', 'paused', 'completed', 'failed', 'cancelled')))
);


--
-- Name: TABLE "workflow_runs"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE "public"."workflow_runs" IS 'Per-invocation record of workflow execution — history + audit trail (#127).';


--
-- Name: COLUMN "workflow_runs"."run_id"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."workflow_runs"."run_id" IS 'Format: wfr-YYYYMMDD-<uuid8>.';


--
-- Name: COLUMN "workflow_runs"."workflow_source"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."workflow_runs"."workflow_source" IS 'file = workflows/*/WORKFLOW.md; custom = DB-backed row in custom_workflows.';


--
-- Name: COLUMN "workflow_runs"."deleted_at"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."workflow_runs"."deleted_at" IS 'When an operator removed this run from the listings. Null is the live case. The row and its ledger are kept: a deleted run is one nobody wants to see, not one nobody may audit.';


--
-- Name: COLUMN "workflow_runs"."outcome"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."workflow_runs"."outcome" IS 'The agent-layer terminal, when the bridge wrote it. Null when this side finalized the run itself.';


--
-- Name: COLUMN "workflow_runs"."reason"; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN "public"."workflow_runs"."reason" IS 'Why the agent layer ended the run. Not the error column: that stays the crash the console renders in red.';


--
-- Name: agent_directives id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."agent_directives" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."agent_directives_id_seq"'::"regclass");


--
-- Name: ai_decision_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_decision_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."ai_decision_logs_id_seq"'::"regclass");


--
-- Name: case_attachments attachment_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_attachments" ALTER COLUMN "attachment_id" SET DEFAULT "nextval"('"public"."case_attachments_attachment_id_seq"'::"regclass");


--
-- Name: case_audit_logs audit_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_audit_logs" ALTER COLUMN "audit_id" SET DEFAULT "nextval"('"public"."case_audit_logs_audit_id_seq"'::"regclass");


--
-- Name: case_comments comment_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_comments" ALTER COLUMN "comment_id" SET DEFAULT "nextval"('"public"."case_comments_comment_id_seq"'::"regclass");


--
-- Name: case_escalations escalation_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_escalations" ALTER COLUMN "escalation_id" SET DEFAULT "nextval"('"public"."case_escalations_escalation_id_seq"'::"regclass");


--
-- Name: case_evidence evidence_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_evidence" ALTER COLUMN "evidence_id" SET DEFAULT "nextval"('"public"."case_evidence_evidence_id_seq"'::"regclass");


--
-- Name: case_iocs ioc_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_iocs" ALTER COLUMN "ioc_id" SET DEFAULT "nextval"('"public"."case_iocs_ioc_id_seq"'::"regclass");


--
-- Name: case_notifications notification_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_notifications" ALTER COLUMN "notification_id" SET DEFAULT "nextval"('"public"."case_notifications_notification_id_seq"'::"regclass");


--
-- Name: case_relationships relationship_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_relationships" ALTER COLUMN "relationship_id" SET DEFAULT "nextval"('"public"."case_relationships_relationship_id_seq"'::"regclass");


--
-- Name: case_slas sla_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_slas" ALTER COLUMN "sla_id" SET DEFAULT "nextval"('"public"."case_slas_sla_id_seq"'::"regclass");


--
-- Name: case_tasks task_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_tasks" ALTER COLUMN "task_id" SET DEFAULT "nextval"('"public"."case_tasks_task_id_seq"'::"regclass");


--
-- Name: chat_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."chat_messages" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."chat_messages_id_seq"'::"regclass");


--
-- Name: config_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."config_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."config_audit_log_id_seq"'::"regclass");


--
-- Name: episodic_gaps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_gaps" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."episodic_gaps_id_seq"'::"regclass");


--
-- Name: episodic_read_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_read_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."episodic_read_log_id_seq"'::"regclass");


--
-- Name: episodic_sightings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_sightings" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."episodic_sightings_id_seq"'::"regclass");


--
-- Name: episodic_verdict_sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdict_sources" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."episodic_verdict_sources_id_seq"'::"regclass");


--
-- Name: episodic_verdicts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdicts" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."episodic_verdicts_id_seq"'::"regclass");


--
-- Name: intake_triggers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."intake_triggers" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."intake_triggers_id_seq"'::"regclass");


--
-- Name: investigation_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."investigation_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."investigation_logs_id_seq"'::"regclass");


--
-- Name: llm_interaction_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."llm_interaction_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."llm_interaction_logs_id_seq"'::"regclass");


--
-- Name: shared_iocs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."shared_iocs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."shared_iocs_id_seq"'::"regclass");


--
-- Name: sketch_mappings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sketch_mappings" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."sketch_mappings_id_seq"'::"regclass");


--
-- Name: threat_indicators id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."threat_indicators" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."threat_indicators_id_seq"'::"regclass");


--
-- Data for Name: agent_directives; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: agent_events; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: agent_run_leases; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: ai_decision_logs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: ai_model_configs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: approval_actions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_attachments; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_audit_logs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_closure_info; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_comments; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_escalations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_evidence; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_findings; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_iocs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_metrics; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_notifications; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_relationships; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_slas; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_tasks; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: case_templates; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."case_templates" VALUES ('template-malware-001', 'Malware Investigation', 'Standard template for investigating malware incidents', 'malware', 'high', 'open', 'sla-high-default', '[{"order": 1, "title": "Initial Triage", "priority": "high", "description": "Assess malware type and scope"}, {"order": 2, "title": "Isolate Affected Systems", "priority": "critical", "description": "Contain the malware spread"}, {"order": 3, "title": "Collect Evidence", "priority": "high", "description": "Gather malware samples, logs, and artifacts"}, {"order": 4, "title": "Analyze Malware", "priority": "medium", "description": "Perform static and dynamic analysis"}, {"order": 5, "title": "Extract IOCs", "priority": "medium", "description": "Identify indicators of compromise"}, {"order": 6, "title": "Threat Hunting", "priority": "high", "description": "Hunt for additional compromised systems"}, {"order": 7, "title": "Remediation", "priority": "high", "description": "Remove malware and restore systems"}, {"order": 8, "title": "Post-Incident Review", "priority": "low", "description": "Document lessons learned"}]', '[{"name": "Identify Malware Type", "step": 1, "actions": ["Review AV alerts", "Analyze file properties"]}, {"name": "Containment", "step": 2, "actions": ["Network isolation", "Disable user accounts"]}, {"name": "Eradication", "step": 3, "actions": ["Remove malware", "Patch vulnerabilities"]}, {"name": "Recovery", "step": 4, "actions": ["Restore from backup", "Monitor for reinfection"]}]', '{T1059,T1055,T1486,T1566}', '{malware,incident-response}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-phishing-001', 'Phishing Investigation', 'Standard template for investigating phishing attacks', 'phishing', 'medium', 'open', 'sla-medium-default', '[{"order": 1, "title": "Verify Phishing Report", "priority": "high", "description": "Confirm the email is malicious"}, {"order": 2, "title": "Identify Affected Users", "priority": "high", "description": "Find who received the phishing email"}, {"order": 3, "title": "Analyze Email", "priority": "medium", "description": "Extract headers, links, and attachments"}, {"order": 4, "title": "Block IOCs", "priority": "high", "description": "Block malicious URLs, domains, and IPs"}, {"order": 5, "title": "Search for Compromised Accounts", "priority": "high", "description": "Check for successful credential theft"}, {"order": 6, "title": "User Education", "priority": "medium", "description": "Notify and educate affected users"}, {"order": 7, "title": "Update Email Filters", "priority": "low", "description": "Add rules to prevent similar attacks"}]', '[{"name": "Triage", "step": 1, "actions": ["Review email headers", "Check sender reputation"]}, {"name": "Containment", "step": 2, "actions": ["Delete emails from inboxes", "Block sender domain"]}, {"name": "Investigation", "step": 3, "actions": ["Analyze attachments/links", "Check for credential use"]}, {"name": "Communication", "step": 4, "actions": ["Alert users", "Security awareness reminder"]}]', '{T1566.001,T1566.002,T1204}', '{phishing,email-security}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-data-exfiltration-001', 'Data Exfiltration Investigation', 'Template for investigating potential data exfiltration', 'data_exfiltration', 'critical', 'open', 'sla-critical-default', '[{"order": 1, "title": "Identify Data Scope", "priority": "critical", "description": "Determine what data was accessed/exfiltrated"}, {"order": 2, "title": "Identify Threat Actor", "priority": "high", "description": "Determine who performed the exfiltration"}, {"order": 3, "title": "Block Exfiltration Channels", "priority": "critical", "description": "Prevent further data loss"}, {"order": 4, "title": "Preserve Evidence", "priority": "high", "description": "Collect logs, network captures, and artifacts"}, {"order": 5, "title": "Assess Impact", "priority": "high", "description": "Evaluate business and legal impact"}, {"order": 6, "title": "Legal/Compliance Notification", "priority": "critical", "description": "Notify legal team and regulators if required"}, {"order": 7, "title": "Containment", "priority": "high", "description": "Revoke access, rotate credentials"}, {"order": 8, "title": "Forensic Analysis", "priority": "medium", "description": "Deep dive investigation"}]', '[{"name": "Detection", "step": 1, "actions": ["Review DLP alerts", "Analyze network traffic"]}, {"name": "Containment", "step": 2, "actions": ["Block C2 communications", "Isolate affected systems"]}, {"name": "Investigation", "step": 3, "actions": ["Timeline reconstruction", "Identify attack vector"]}, {"name": "Recovery", "step": 4, "actions": ["Restore access controls", "Implement monitoring"]}]', '{T1048,T1041,T1567,T1020}', '{data-breach,exfiltration,critical}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-insider-threat-001', 'Insider Threat Investigation', 'Template for investigating insider threat activities', 'insider_threat', 'high', 'open', 'sla-high-default', '[{"order": 1, "title": "Initial Assessment", "priority": "high", "description": "Review alert and user activity"}, {"order": 2, "title": "Preserve Evidence", "priority": "critical", "description": "Collect user activity logs and data"}, {"order": 3, "title": "Coordinate with HR/Legal", "priority": "high", "description": "Involve HR and legal team"}, {"order": 4, "title": "Analyze User Behavior", "priority": "high", "description": "Review access patterns and anomalies"}, {"order": 5, "title": "Assess Risk", "priority": "high", "description": "Determine potential damage and intent"}, {"order": 6, "title": "Containment Actions", "priority": "medium", "description": "Restrict access if necessary"}, {"order": 7, "title": "Interview Preparation", "priority": "medium", "description": "Prepare evidence for user interview"}, {"order": 8, "title": "Remediation", "priority": "low", "description": "Implement controls to prevent recurrence"}]', '[{"name": "Detection", "step": 1, "actions": ["UEBA alert review", "Access log analysis"]}, {"name": "Evidence Collection", "step": 2, "actions": ["Covert monitoring", "Endpoint forensics"]}, {"name": "Coordination", "step": 3, "actions": ["HR notification", "Legal consultation"]}, {"name": "Action", "step": 4, "actions": ["Account suspension", "Access revocation"]}]', '{T1078,T1530,T1213}', '{insider-threat,ueba}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-ransomware-001', 'Ransomware Incident', 'Template for responding to ransomware attacks', 'ransomware', 'critical', 'open', 'sla-critical-default', '[{"order": 1, "title": "Activate Incident Response Team", "priority": "critical", "description": "Assemble full IR team"}, {"order": 2, "title": "Isolate Affected Systems", "priority": "critical", "description": "Prevent ransomware spread"}, {"order": 3, "title": "Identify Ransomware Variant", "priority": "high", "description": "Determine ransomware family"}, {"order": 4, "title": "Assess Encryption Scope", "priority": "high", "description": "Identify all encrypted systems/files"}, {"order": 5, "title": "Check Backups", "priority": "critical", "description": "Verify backup integrity and coverage"}, {"order": 6, "title": "Collect Ransom Note", "priority": "medium", "description": "Preserve ransom demands and payment info"}, {"order": 7, "title": "Notify Stakeholders", "priority": "high", "description": "Alert executive team, legal, PR"}, {"order": 8, "title": "Containment", "priority": "critical", "description": "Stop the spread and preserve evidence"}, {"order": 9, "title": "Recovery Planning", "priority": "high", "description": "Develop restoration strategy"}, {"order": 10, "title": "System Restoration", "priority": "high", "description": "Restore from backups or rebuild"}]', '[{"name": "Initial Response", "step": 1, "actions": ["Network segmentation", "Disable RDP/SMB"]}, {"name": "Assessment", "step": 2, "actions": ["Identify patient zero", "Map encryption scope"]}, {"name": "Containment", "step": 3, "actions": ["Isolate systems", "Kill processes"]}, {"name": "Eradication", "step": 4, "actions": ["Remove ransomware", "Patch vulnerabilities"]}, {"name": "Recovery", "step": 5, "actions": ["Restore from backup", "Verify integrity"]}]', '{T1486,T1490,T1489,T1562}', '{ransomware,critical,business-continuity}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-account-compromise-001', 'Account Compromise', 'Template for investigating compromised user accounts', 'account_compromise', 'high', 'open', 'sla-high-default', '[{"order": 1, "title": "Verify Compromise", "priority": "high", "description": "Confirm account compromise indicators"}, {"order": 2, "title": "Disable Account", "priority": "critical", "description": "Temporarily disable compromised account"}, {"order": 3, "title": "Review Account Activity", "priority": "high", "description": "Analyze login history and actions"}, {"order": 4, "title": "Identify Attack Vector", "priority": "high", "description": "Determine how account was compromised"}, {"order": 5, "title": "Check for Lateral Movement", "priority": "high", "description": "Hunt for additional compromised accounts"}, {"order": 6, "title": "Reset Credentials", "priority": "high", "description": "Force password reset and revoke tokens"}, {"order": 7, "title": "Enable MFA", "priority": "medium", "description": "Ensure MFA is enabled for account"}, {"order": 8, "title": "User Notification", "priority": "medium", "description": "Contact account owner"}]', '[{"name": "Detection", "step": 1, "actions": ["Impossible travel", "Anomalous access"]}, {"name": "Containment", "step": 2, "actions": ["Disable account", "Revoke sessions"]}, {"name": "Investigation", "step": 3, "actions": ["Review logs", "Check for data access"]}, {"name": "Recovery", "step": 4, "actions": ["Reset password", "Enable MFA", "Monitor"]}]', '{T1078,T1110,T1552}', '{account-compromise,credential-theft}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-ddos-001', 'DDoS Attack Response', 'Template for responding to distributed denial of service attacks', 'ddos', 'critical', 'open', 'sla-critical-default', '[{"order": 1, "title": "Confirm DDoS Attack", "priority": "critical", "description": "Verify attack type and scale"}, {"order": 2, "title": "Activate DDoS Mitigation", "priority": "critical", "description": "Enable DDoS protection services"}, {"order": 3, "title": "Notify ISP/CDN", "priority": "high", "description": "Contact service providers for assistance"}, {"order": 4, "title": "Analyze Traffic Patterns", "priority": "high", "description": "Identify attack vectors and sources"}, {"order": 5, "title": "Implement Filtering Rules", "priority": "high", "description": "Block malicious traffic"}, {"order": 6, "title": "Monitor Service Availability", "priority": "high", "description": "Track mitigation effectiveness"}, {"order": 7, "title": "Communicate with Stakeholders", "priority": "medium", "description": "Update business on status"}, {"order": 8, "title": "Post-Attack Analysis", "priority": "low", "description": "Review and improve defenses"}]', '[{"name": "Detection", "step": 1, "actions": ["Monitor traffic spikes", "Service health checks"]}, {"name": "Mitigation", "step": 2, "actions": ["Enable rate limiting", "Activate DDoS protection"]}, {"name": "Traffic Analysis", "step": 3, "actions": ["Identify attack type", "Block sources"]}, {"name": "Recovery", "step": 4, "actions": ["Restore services", "Review capacity"]}]', '{T1498,T1499}', '{ddos,availability,network-attack}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."case_templates" VALUES ('template-webapp-attack-001', 'Web Application Attack', 'Template for investigating web application attacks (SQL injection, XSS, etc.)', 'web_attack', 'high', 'open', 'sla-high-default', '[{"order": 1, "title": "Identify Attack Type", "priority": "high", "description": "Determine attack vector (SQLi, XSS, etc.)"}, {"order": 2, "title": "Assess Impact", "priority": "critical", "description": "Check if attack was successful"}, {"order": 3, "title": "Block Attack Source", "priority": "high", "description": "Block malicious IP addresses"}, {"order": 4, "title": "Review WAF Logs", "priority": "high", "description": "Analyze web application firewall logs"}, {"order": 5, "title": "Check for Data Breach", "priority": "critical", "description": "Verify if data was accessed/exfiltrated"}, {"order": 6, "title": "Patch Vulnerability", "priority": "high", "description": "Fix exploited vulnerability"}, {"order": 7, "title": "Update WAF Rules", "priority": "medium", "description": "Add rules to prevent similar attacks"}, {"order": 8, "title": "Vulnerability Scan", "priority": "medium", "description": "Scan for additional vulnerabilities"}]', '[{"name": "Detection", "step": 1, "actions": ["WAF alerts", "Anomalous requests"]}, {"name": "Analysis", "step": 2, "actions": ["Review attack payload", "Check database logs"]}, {"name": "Containment", "step": 3, "actions": ["Block attacker", "Disable vulnerable endpoint"]}, {"name": "Remediation", "step": 4, "actions": ["Patch code", "Update WAF", "Pentest"]}]', '{T1190,T1211,T1505}', '{web-attack,application-security}', true, 0, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');


--
-- Data for Name: case_watchers; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: cases; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: chat_messages; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: config_audit_log; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: conversations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: custom_agents; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: custom_workflows; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_distil_failures; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_distil_markers; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_gaps; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_read_log; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_sightings; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_verdict_sources; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: episodic_verdicts; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: federation_sources; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: finding_mitre_predictions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: findings; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: intake_triggers; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: integration_configs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: investigation_logs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: investigations; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: ip_exclusions; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: llm_interaction_logs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: llm_provider_configs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: mcp_credentials; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: roles; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."roles" VALUES ('role-viewer', 'Viewer', 'Read-only access to findings and cases', '{"cases.read": true, "users.read": false, "ai_chat.use": false, "findings.read": true, "settings.read": false, "integrations.read": false, "ai_decisions.approve": false}', true, '2026-10-22 11:01:51.148984', '2026-10-22 11:01:51.148984');
INSERT INTO "public"."roles" VALUES ('role-analyst', 'Analyst', 'Full access to findings and cases, limited integrations', '{"cases.read": true, "loglm.view": true, "users.read": false, "ai_chat.use": true, "cases.write": true, "cases.assign": false, "cases.delete": false, "findings.read": true, "settings.read": true, "findings.write": true, "settings.write": false, "findings.delete": false, "integrations.read": true, "integrations.write": false, "ai_decisions.approve": false}', true, '2026-10-22 11:01:51.148984', '2026-10-22 11:01:53.006901');
INSERT INTO "public"."roles" VALUES ('role-senior-analyst', 'Senior Analyst', 'Full analyst access plus approval rights', '{"cases.read": true, "loglm.view": true, "users.read": true, "ai_chat.use": true, "cases.write": true, "cases.assign": true, "cases.delete": false, "findings.read": true, "settings.read": true, "findings.write": true, "settings.write": false, "findings.delete": true, "integrations.read": true, "integrations.write": true, "ai_decisions.approve": true}', true, '2026-10-22 11:01:51.148984', '2026-10-22 11:01:53.006901');
INSERT INTO "public"."roles" VALUES ('role-manager', 'Manager', 'User management and all integrations', '{"cases.read": true, "loglm.view": true, "users.read": true, "ai_chat.use": true, "cases.write": true, "users.write": true, "cases.assign": true, "cases.delete": true, "users.delete": false, "findings.read": true, "settings.read": true, "findings.write": true, "settings.write": true, "findings.delete": true, "integrations.read": true, "integrations.write": true, "ai_decisions.approve": true}', true, '2026-10-22 11:01:51.148984', '2026-10-22 11:01:53.006901');
INSERT INTO "public"."roles" VALUES ('role-admin', 'Admin', 'Full system access', '{"cases.read": true, "loglm.view": true, "users.read": true, "ai_chat.use": true, "cases.write": true, "users.write": true, "cases.assign": true, "cases.delete": true, "users.delete": true, "findings.read": true, "settings.read": true, "findings.write": true, "settings.write": true, "findings.delete": true, "integrations.read": true, "integrations.write": true, "ai_decisions.approve": true}', true, '2026-10-22 11:01:51.148984', '2026-10-22 11:01:53.006901');


--
-- Data for Name: shared_iocs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: sketch_mappings; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: sla_policies; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."sla_policies" VALUES ('sla-critical-default', 'Critical Priority SLA', 'Standard SLA for critical priority cases requiring immediate attention', 'critical', 1, 4, false, NULL, '{75,90,100}', true, true, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."sla_policies" VALUES ('sla-high-default', 'High Priority SLA', 'Standard SLA for high priority cases', 'high', 2, 8, false, NULL, '{75,90,100}', true, true, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."sla_policies" VALUES ('sla-medium-default', 'Medium Priority SLA', 'Standard SLA for medium priority cases', 'medium', 4, 24, true, NULL, '{75,90,100}', true, true, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');
INSERT INTO "public"."sla_policies" VALUES ('sla-low-default', 'Low Priority SLA', 'Standard SLA for low priority cases', 'low', 8, 72, true, NULL, '{75,90,100}', true, true, '2026-10-22 11:01:57.769556', '2026-10-22 11:01:57.769556');


--
-- Data for Name: system_config; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."system_config" VALUES ('general.settings', '{"theme": "dark", "enable_keyring": false, "auto_start_sync": false, "show_notifications": true}', 'General application settings', 'general', NULL, '2026-10-22 11:01:50.758758', '2026-10-22 11:01:50.758758');
INSERT INTO "public"."system_config" VALUES ('approval.force_manual_approval', '{"enabled": false}', 'Force manual approval for all actions regardless of confidence score', 'approval', NULL, '2026-10-22 11:01:50.759803', '2026-10-22 11:01:50.759803');
INSERT INTO "public"."system_config" VALUES ('theme.current', '{"theme": "dark"}', 'Current UI theme', 'theme', NULL, '2026-10-22 11:01:50.760411', '2026-10-22 11:01:50.760411');
INSERT INTO "public"."system_config" VALUES ('federation.settings', '{"enabled": false}', 'Federated monitoring global on/off (per-source toggles live in federation_sources)', 'federation', NULL, '2026-10-22 11:01:52.634497', '2026-10-22 11:01:52.634497');
INSERT INTO "public"."system_config" VALUES ('bifrost.virtual_keys', '{"default_vk": "", "budget_limit_usd": 0, "enforcement_mode": "warning"}', 'Bifrost virtual-key configuration and budget settings (#186)', 'ai', NULL, '2026-10-22 11:01:52.806074', '2026-10-22 11:01:52.806074');
INSERT INTO "public"."system_config" VALUES ('schema_version', '{"version": "0.7.0"}', 'Vigil version this schema was last provisioned at', 'system', NULL, '2026-10-22 11:01:57.794545', '2026-10-22 11:01:57.794545');


--
-- Data for Name: threat_indicators; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: user_preferences; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO "public"."user_preferences" VALUES ('default', '{"theme": "dark", "auto_start_sync": false, "show_notifications": true}', 'Default User', NULL, '2026-10-22 11:01:50.760997', '2026-10-22 11:01:50.760997', NULL);


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: workflow_run_phases; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: workflow_runs; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Name: agent_directives_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."agent_directives_id_seq"', 1, false);


--
-- Name: ai_decision_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."ai_decision_logs_id_seq"', 1, false);


--
-- Name: case_attachments_attachment_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_attachments_attachment_id_seq"', 1, false);


--
-- Name: case_audit_logs_audit_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_audit_logs_audit_id_seq"', 1, false);


--
-- Name: case_comments_comment_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_comments_comment_id_seq"', 1, false);


--
-- Name: case_escalations_escalation_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_escalations_escalation_id_seq"', 1, false);


--
-- Name: case_evidence_evidence_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_evidence_evidence_id_seq"', 1, false);


--
-- Name: case_iocs_ioc_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_iocs_ioc_id_seq"', 1, false);


--
-- Name: case_notifications_notification_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_notifications_notification_id_seq"', 1, false);


--
-- Name: case_relationships_relationship_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_relationships_relationship_id_seq"', 1, false);


--
-- Name: case_slas_sla_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_slas_sla_id_seq"', 1, false);


--
-- Name: case_tasks_task_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."case_tasks_task_id_seq"', 1, false);


--
-- Name: chat_messages_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."chat_messages_id_seq"', 1, false);


--
-- Name: config_audit_log_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."config_audit_log_id_seq"', 1, false);


--
-- Name: episodic_gaps_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."episodic_gaps_id_seq"', 1, false);


--
-- Name: episodic_read_log_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."episodic_read_log_id_seq"', 1, false);


--
-- Name: episodic_sightings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."episodic_sightings_id_seq"', 1, false);


--
-- Name: episodic_verdict_sources_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."episodic_verdict_sources_id_seq"', 1, false);


--
-- Name: episodic_verdicts_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."episodic_verdicts_id_seq"', 1, false);


--
-- Name: intake_triggers_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."intake_triggers_id_seq"', 1, false);


--
-- Name: investigation_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."investigation_logs_id_seq"', 1, false);


--
-- Name: llm_interaction_logs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."llm_interaction_logs_id_seq"', 1, false);


--
-- Name: shared_iocs_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."shared_iocs_id_seq"', 1, false);


--
-- Name: sketch_mappings_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."sketch_mappings_id_seq"', 1, false);


--
-- Name: threat_indicators_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('"public"."threat_indicators_id_seq"', 1, false);


--
-- Name: agent_directives agent_directives_directive_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."agent_directives"
    ADD CONSTRAINT "agent_directives_directive_id_key" UNIQUE ("directive_id");


--
-- Name: agent_directives agent_directives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."agent_directives"
    ADD CONSTRAINT "agent_directives_pkey" PRIMARY KEY ("id");


--
-- Name: agent_events agent_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."agent_events"
    ADD CONSTRAINT "agent_events_pkey" PRIMARY KEY ("run_id", "seq");


--
-- Name: agent_run_leases agent_run_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."agent_run_leases"
    ADD CONSTRAINT "agent_run_leases_pkey" PRIMARY KEY ("run_id");


--
-- Name: ai_decision_logs ai_decision_logs_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_decision_logs"
    ADD CONSTRAINT "ai_decision_logs_decision_id_key" UNIQUE ("decision_id");


--
-- Name: ai_decision_logs ai_decision_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_decision_logs"
    ADD CONSTRAINT "ai_decision_logs_pkey" PRIMARY KEY ("id");


--
-- Name: ai_model_configs ai_model_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_model_configs"
    ADD CONSTRAINT "ai_model_configs_pkey" PRIMARY KEY ("component");


--
-- Name: approval_actions approval_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."approval_actions"
    ADD CONSTRAINT "approval_actions_pkey" PRIMARY KEY ("action_id");


--
-- Name: case_attachments case_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_attachments"
    ADD CONSTRAINT "case_attachments_pkey" PRIMARY KEY ("attachment_id");


--
-- Name: case_audit_logs case_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_audit_logs"
    ADD CONSTRAINT "case_audit_logs_pkey" PRIMARY KEY ("audit_id");


--
-- Name: case_closure_info case_closure_info_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_closure_info"
    ADD CONSTRAINT "case_closure_info_pkey" PRIMARY KEY ("case_id");


--
-- Name: case_comments case_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_comments"
    ADD CONSTRAINT "case_comments_pkey" PRIMARY KEY ("comment_id");


--
-- Name: case_escalations case_escalations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_escalations"
    ADD CONSTRAINT "case_escalations_pkey" PRIMARY KEY ("escalation_id");


--
-- Name: case_evidence case_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_evidence"
    ADD CONSTRAINT "case_evidence_pkey" PRIMARY KEY ("evidence_id");


--
-- Name: case_findings case_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_findings"
    ADD CONSTRAINT "case_findings_pkey" PRIMARY KEY ("case_id", "finding_id");


--
-- Name: case_iocs case_iocs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_iocs"
    ADD CONSTRAINT "case_iocs_pkey" PRIMARY KEY ("ioc_id");


--
-- Name: case_metrics case_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_metrics"
    ADD CONSTRAINT "case_metrics_pkey" PRIMARY KEY ("case_id");


--
-- Name: case_notifications case_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_notifications"
    ADD CONSTRAINT "case_notifications_pkey" PRIMARY KEY ("notification_id");


--
-- Name: case_relationships case_relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_relationships"
    ADD CONSTRAINT "case_relationships_pkey" PRIMARY KEY ("relationship_id");


--
-- Name: case_slas case_slas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_slas"
    ADD CONSTRAINT "case_slas_pkey" PRIMARY KEY ("sla_id");


--
-- Name: case_tasks case_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_tasks"
    ADD CONSTRAINT "case_tasks_pkey" PRIMARY KEY ("task_id");


--
-- Name: case_templates case_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_templates"
    ADD CONSTRAINT "case_templates_pkey" PRIMARY KEY ("template_id");


--
-- Name: case_watchers case_watchers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_watchers"
    ADD CONSTRAINT "case_watchers_pkey" PRIMARY KEY ("case_id", "user_id");


--
-- Name: cases cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."cases"
    ADD CONSTRAINT "cases_pkey" PRIMARY KEY ("case_id");


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id");


--
-- Name: config_audit_log config_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."config_audit_log"
    ADD CONSTRAINT "config_audit_log_pkey" PRIMARY KEY ("id");


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."conversations"
    ADD CONSTRAINT "conversations_pkey" PRIMARY KEY ("id");


--
-- Name: custom_agents custom_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."custom_agents"
    ADD CONSTRAINT "custom_agents_pkey" PRIMARY KEY ("id");


--
-- Name: custom_workflows custom_workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."custom_workflows"
    ADD CONSTRAINT "custom_workflows_pkey" PRIMARY KEY ("workflow_id");


--
-- Name: episodic_distil_failures episodic_distil_failures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_distil_failures"
    ADD CONSTRAINT "episodic_distil_failures_pkey" PRIMARY KEY ("investigation_kind", "failure_key");


--
-- Name: episodic_distil_markers episodic_distil_markers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_distil_markers"
    ADD CONSTRAINT "episodic_distil_markers_pkey" PRIMARY KEY ("investigation_kind", "investigation_id");


--
-- Name: episodic_gaps episodic_gaps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_gaps"
    ADD CONSTRAINT "episodic_gaps_pkey" PRIMARY KEY ("id");


--
-- Name: episodic_gaps episodic_gaps_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_gaps"
    ADD CONSTRAINT "episodic_gaps_unique" UNIQUE ("investigation_kind", "investigation_id", "hypothesis_id");


--
-- Name: episodic_read_log episodic_read_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_read_log"
    ADD CONSTRAINT "episodic_read_log_pkey" PRIMARY KEY ("id");


--
-- Name: episodic_sightings episodic_sightings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_sightings"
    ADD CONSTRAINT "episodic_sightings_pkey" PRIMARY KEY ("id");


--
-- Name: episodic_sightings episodic_sightings_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_sightings"
    ADD CONSTRAINT "episodic_sightings_unique" UNIQUE ("entity_key", "investigation_kind", "investigation_id", "source_system");


--
-- Name: episodic_verdict_sources episodic_verdict_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdict_sources"
    ADD CONSTRAINT "episodic_verdict_sources_pkey" PRIMARY KEY ("id");


--
-- Name: episodic_verdict_sources episodic_verdict_sources_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdict_sources"
    ADD CONSTRAINT "episodic_verdict_sources_unique" UNIQUE ("verdict_id", "source_system");


--
-- Name: episodic_verdicts episodic_verdicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdicts"
    ADD CONSTRAINT "episodic_verdicts_pkey" PRIMARY KEY ("id");


--
-- Name: episodic_verdicts episodic_verdicts_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdicts"
    ADD CONSTRAINT "episodic_verdicts_unique" UNIQUE ("investigation_kind", "investigation_id", "hypothesis_id");


--
-- Name: federation_sources federation_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."federation_sources"
    ADD CONSTRAINT "federation_sources_pkey" PRIMARY KEY ("source_id");


--
-- Name: finding_mitre_predictions finding_mitre_predictions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."finding_mitre_predictions"
    ADD CONSTRAINT "finding_mitre_predictions_pkey" PRIMARY KEY ("finding_id", "technique_id");


--
-- Name: findings findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."findings"
    ADD CONSTRAINT "findings_pkey" PRIMARY KEY ("finding_id");


--
-- Name: intake_triggers intake_triggers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."intake_triggers"
    ADD CONSTRAINT "intake_triggers_pkey" PRIMARY KEY ("id");


--
-- Name: integration_configs integration_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."integration_configs"
    ADD CONSTRAINT "integration_configs_pkey" PRIMARY KEY ("integration_id");


--
-- Name: investigation_logs investigation_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."investigation_logs"
    ADD CONSTRAINT "investigation_logs_pkey" PRIMARY KEY ("id");


--
-- Name: investigations investigations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."investigations"
    ADD CONSTRAINT "investigations_pkey" PRIMARY KEY ("investigation_id");


--
-- Name: ip_exclusions ip_exclusions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ip_exclusions"
    ADD CONSTRAINT "ip_exclusions_pkey" PRIMARY KEY ("exclusion_id");


--
-- Name: llm_interaction_logs llm_interaction_logs_interaction_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."llm_interaction_logs"
    ADD CONSTRAINT "llm_interaction_logs_interaction_id_key" UNIQUE ("interaction_id");


--
-- Name: llm_interaction_logs llm_interaction_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."llm_interaction_logs"
    ADD CONSTRAINT "llm_interaction_logs_pkey" PRIMARY KEY ("id");


--
-- Name: llm_provider_configs llm_provider_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."llm_provider_configs"
    ADD CONSTRAINT "llm_provider_configs_pkey" PRIMARY KEY ("provider_id");


--
-- Name: mcp_credentials mcp_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."mcp_credentials"
    ADD CONSTRAINT "mcp_credentials_pkey" PRIMARY KEY ("credential_id");


--
-- Name: mcp_credentials mcp_credentials_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."mcp_credentials"
    ADD CONSTRAINT "mcp_credentials_token_hash_key" UNIQUE ("token_hash");


--
-- Name: roles roles_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roles"
    ADD CONSTRAINT "roles_name_key" UNIQUE ("name");


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."roles"
    ADD CONSTRAINT "roles_pkey" PRIMARY KEY ("role_id");


--
-- Name: shared_iocs shared_iocs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."shared_iocs"
    ADD CONSTRAINT "shared_iocs_pkey" PRIMARY KEY ("id");


--
-- Name: sketch_mappings sketch_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sketch_mappings"
    ADD CONSTRAINT "sketch_mappings_pkey" PRIMARY KEY ("id");


--
-- Name: sla_policies sla_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sla_policies"
    ADD CONSTRAINT "sla_policies_pkey" PRIMARY KEY ("policy_id");


--
-- Name: system_config system_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."system_config"
    ADD CONSTRAINT "system_config_pkey" PRIMARY KEY ("key");


--
-- Name: threat_indicators threat_indicators_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."threat_indicators"
    ADD CONSTRAINT "threat_indicators_pkey" PRIMARY KEY ("id");


--
-- Name: threat_indicators threat_indicators_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."threat_indicators"
    ADD CONSTRAINT "threat_indicators_unique" UNIQUE ("source", "indicator_type", "indicator_value");


--
-- Name: chat_messages uq_chat_messages_conv_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "uq_chat_messages_conv_seq" UNIQUE ("conversation_id", "seq");


--
-- Name: user_preferences user_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."user_preferences"
    ADD CONSTRAINT "user_preferences_pkey" PRIMARY KEY ("user_id");


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_email_key" UNIQUE ("email");


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_pkey" PRIMARY KEY ("user_id");


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_username_key" UNIQUE ("username");


--
-- Name: workflow_run_phases workflow_run_phases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."workflow_run_phases"
    ADD CONSTRAINT "workflow_run_phases_pkey" PRIMARY KEY ("run_id", "phase_id");


--
-- Name: workflow_runs workflow_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."workflow_runs"
    ADD CONSTRAINT "workflow_runs_pkey" PRIMARY KEY ("run_id");


--
-- Name: idx_agent_directives_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_agent_directives_run" ON "public"."agent_directives" USING "btree" ("run_id", "id");


--
-- Name: idx_agent_events_terminal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_agent_events_terminal" ON "public"."agent_events" USING "btree" ("run_id") WHERE ("kind" = 'terminal'::"text");


--
-- Name: idx_agent_run_leases_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_agent_run_leases_due" ON "public"."agent_run_leases" USING "btree" ("claim_until");


--
-- Name: idx_ai_decision_actual_outcome; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_actual_outcome" ON "public"."ai_decision_logs" USING "btree" ("actual_outcome");


--
-- Name: idx_ai_decision_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_agent_id" ON "public"."ai_decision_logs" USING "btree" ("agent_id");


--
-- Name: idx_ai_decision_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_case_id" ON "public"."ai_decision_logs" USING "btree" ("case_id");


--
-- Name: idx_ai_decision_finding_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_finding_id" ON "public"."ai_decision_logs" USING "btree" ("finding_id");


--
-- Name: idx_ai_decision_human_decision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_human_decision" ON "public"."ai_decision_logs" USING "btree" ("human_decision");


--
-- Name: idx_ai_decision_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_decision_timestamp" ON "public"."ai_decision_logs" USING "btree" ("timestamp");


--
-- Name: idx_ai_model_configs_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ai_model_configs_provider" ON "public"."ai_model_configs" USING "btree" ("provider_id");


--
-- Name: idx_approval_actions_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_approval_actions_pending" ON "public"."approval_actions" USING "btree" ("created_at" DESC) WHERE ((("status")::"text" = 'pending'::"text") AND ("requires_approval" = true));


--
-- Name: idx_approval_actions_status_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_approval_actions_status_created" ON "public"."approval_actions" USING "btree" ("status", "created_at" DESC);


--
-- Name: idx_approval_actions_workflow_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_approval_actions_workflow_run" ON "public"."approval_actions" USING "btree" ("workflow_run_id") WHERE ("workflow_run_id" IS NOT NULL);


--
-- Name: idx_audit_changed_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_changed_by" ON "public"."config_audit_log" USING "btree" ("changed_by");


--
-- Name: idx_audit_config_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_config_key" ON "public"."config_audit_log" USING "btree" ("config_key");


--
-- Name: idx_audit_config_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_config_type" ON "public"."config_audit_log" USING "btree" ("config_type");


--
-- Name: idx_audit_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_timestamp" ON "public"."config_audit_log" USING "btree" ("timestamp");


--
-- Name: idx_case_assignee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_assignee" ON "public"."cases" USING "btree" ("assignee");


--
-- Name: idx_case_attachment_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_attachment_case_id" ON "public"."case_attachments" USING "btree" ("case_id");


--
-- Name: idx_case_attachment_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_attachment_created_at" ON "public"."case_attachments" USING "btree" ("created_at");


--
-- Name: idx_case_attachment_uploaded_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_attachment_uploaded_by" ON "public"."case_attachments" USING "btree" ("uploaded_by");


--
-- Name: idx_case_audit_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_audit_action" ON "public"."case_audit_logs" USING "btree" ("action");


--
-- Name: idx_case_audit_changed_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_audit_changed_by" ON "public"."case_audit_logs" USING "btree" ("changed_by");


--
-- Name: idx_case_audit_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_audit_entity_id" ON "public"."case_audit_logs" USING "btree" ("entity_id");


--
-- Name: idx_case_audit_entity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_audit_entity_type" ON "public"."case_audit_logs" USING "btree" ("entity_type");


--
-- Name: idx_case_audit_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_audit_timestamp" ON "public"."case_audit_logs" USING "btree" ("timestamp");


--
-- Name: idx_case_comment_author; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_comment_author" ON "public"."case_comments" USING "btree" ("author");


--
-- Name: idx_case_comment_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_comment_case_id" ON "public"."case_comments" USING "btree" ("case_id");


--
-- Name: idx_case_comment_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_comment_created_at" ON "public"."case_comments" USING "btree" ("created_at");


--
-- Name: idx_case_comment_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_comment_parent_id" ON "public"."case_comments" USING "btree" ("parent_comment_id");


--
-- Name: idx_case_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_created_at" ON "public"."cases" USING "btree" ("created_at");


--
-- Name: idx_case_escalation_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_escalation_case_id" ON "public"."case_escalations" USING "btree" ("case_id");


--
-- Name: idx_case_escalation_escalated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_escalation_escalated_at" ON "public"."case_escalations" USING "btree" ("escalated_at");


--
-- Name: idx_case_escalation_escalated_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_escalation_escalated_to" ON "public"."case_escalations" USING "btree" ("escalated_to");


--
-- Name: idx_case_escalation_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_escalation_status" ON "public"."case_escalations" USING "btree" ("status");


--
-- Name: idx_case_evidence_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_evidence_case_id" ON "public"."case_evidence" USING "btree" ("case_id");


--
-- Name: idx_case_evidence_collected_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_evidence_collected_at" ON "public"."case_evidence" USING "btree" ("collected_at");


--
-- Name: idx_case_evidence_collected_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_evidence_collected_by" ON "public"."case_evidence" USING "btree" ("collected_by");


--
-- Name: idx_case_evidence_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_evidence_type" ON "public"."case_evidence" USING "btree" ("evidence_type");


--
-- Name: idx_case_findings_finding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_findings_finding" ON "public"."case_findings" USING "btree" ("finding_id", "case_id");


--
-- Name: idx_case_ioc_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_ioc_case_id" ON "public"."case_iocs" USING "btree" ("case_id");


--
-- Name: idx_case_ioc_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_ioc_is_active" ON "public"."case_iocs" USING "btree" ("is_active");


--
-- Name: idx_case_ioc_threat_level; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_ioc_threat_level" ON "public"."case_iocs" USING "btree" ("threat_level");


--
-- Name: idx_case_ioc_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_ioc_type" ON "public"."case_iocs" USING "btree" ("ioc_type");


--
-- Name: idx_case_ioc_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_ioc_value" ON "public"."case_iocs" USING "btree" ("value");


--
-- Name: idx_case_notification_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_case_id" ON "public"."case_notifications" USING "btree" ("case_id");


--
-- Name: idx_case_notification_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_created_at" ON "public"."case_notifications" USING "btree" ("created_at");


--
-- Name: idx_case_notification_is_read; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_is_read" ON "public"."case_notifications" USING "btree" ("is_read");


--
-- Name: idx_case_notification_is_sent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_is_sent" ON "public"."case_notifications" USING "btree" ("is_sent");


--
-- Name: idx_case_notification_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_type" ON "public"."case_notifications" USING "btree" ("notification_type");


--
-- Name: idx_case_notification_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_notification_user_id" ON "public"."case_notifications" USING "btree" ("user_id");


--
-- Name: idx_case_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_priority" ON "public"."cases" USING "btree" ("priority");


--
-- Name: idx_case_relationship_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_relationship_case_id" ON "public"."case_relationships" USING "btree" ("case_id");


--
-- Name: idx_case_relationship_related_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_relationship_related_case_id" ON "public"."case_relationships" USING "btree" ("related_case_id");


--
-- Name: idx_case_relationship_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_relationship_type" ON "public"."case_relationships" USING "btree" ("relationship_type");


--
-- Name: idx_case_sla_breached; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_sla_breached" ON "public"."case_slas" USING "btree" ("breached");


--
-- Name: idx_case_sla_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_sla_case_id" ON "public"."case_slas" USING "btree" ("case_id");


--
-- Name: idx_case_sla_policy_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_sla_policy_id" ON "public"."case_slas" USING "btree" ("sla_policy_id");


--
-- Name: idx_case_sla_resolution_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_sla_resolution_due" ON "public"."case_slas" USING "btree" ("resolution_due");


--
-- Name: idx_case_sla_response_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_sla_response_due" ON "public"."case_slas" USING "btree" ("response_due");


--
-- Name: idx_case_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_status" ON "public"."cases" USING "btree" ("status");


--
-- Name: idx_case_task_assignee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_task_assignee" ON "public"."case_tasks" USING "btree" ("assignee");


--
-- Name: idx_case_task_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_task_case_id" ON "public"."case_tasks" USING "btree" ("case_id");


--
-- Name: idx_case_task_due_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_task_due_date" ON "public"."case_tasks" USING "btree" ("due_date");


--
-- Name: idx_case_task_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_task_parent_id" ON "public"."case_tasks" USING "btree" ("parent_task_id");


--
-- Name: idx_case_task_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_task_status" ON "public"."case_tasks" USING "btree" ("status");


--
-- Name: idx_case_template_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_template_active" ON "public"."case_templates" USING "btree" ("is_active");


--
-- Name: idx_case_template_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_template_type" ON "public"."case_templates" USING "btree" ("template_type");


--
-- Name: idx_case_template_usage_count; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_template_usage_count" ON "public"."case_templates" USING "btree" ("usage_count");


--
-- Name: idx_case_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_updated_at" ON "public"."cases" USING "btree" ("updated_at");


--
-- Name: idx_case_watcher_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_watcher_case_id" ON "public"."case_watchers" USING "btree" ("case_id");


--
-- Name: idx_case_watcher_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_case_watcher_user_id" ON "public"."case_watchers" USING "btree" ("user_id");


--
-- Name: idx_chat_messages_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_chat_messages_conversation" ON "public"."chat_messages" USING "btree" ("conversation_id");


--
-- Name: idx_conversations_user_archived; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_conversations_user_archived" ON "public"."conversations" USING "btree" ("user_id", "archived");


--
-- Name: idx_conversations_user_last_msg; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_conversations_user_last_msg" ON "public"."conversations" USING "btree" ("user_id", "last_message_at");


--
-- Name: idx_custom_agents_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_custom_agents_updated_at" ON "public"."custom_agents" USING "btree" ("updated_at" DESC);


--
-- Name: idx_custom_workflows_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_custom_workflows_active" ON "public"."custom_workflows" USING "btree" ("is_active");


--
-- Name: idx_custom_workflows_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_custom_workflows_created_by" ON "public"."custom_workflows" USING "btree" ("created_by");


--
-- Name: idx_custom_workflows_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_custom_workflows_name" ON "public"."custom_workflows" USING "btree" ("name");


--
-- Name: idx_episodic_gaps_investigation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_gaps_investigation" ON "public"."episodic_gaps" USING "btree" ("investigation_kind", "investigation_id");


--
-- Name: idx_episodic_gaps_recall; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_gaps_recall" ON "public"."episodic_gaps" USING "btree" ("concluded_at" DESC, "id");


--
-- Name: idx_episodic_gaps_subjects; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_gaps_subjects" ON "public"."episodic_gaps" USING "gin" ("subject_entities");


--
-- Name: idx_episodic_markers_origin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_markers_origin" ON "public"."episodic_distil_markers" USING "gin" ("origin_run_ids");


--
-- Name: idx_episodic_read_log_keys; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_read_log_keys" ON "public"."episodic_read_log" USING "gin" ("keys");


--
-- Name: idx_episodic_read_log_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_read_log_ts" ON "public"."episodic_read_log" USING "btree" ("ts");


--
-- Name: idx_episodic_sightings_investigation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_sightings_investigation" ON "public"."episodic_sightings" USING "btree" ("investigation_kind", "investigation_id");


--
-- Name: idx_episodic_sightings_recall; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_sightings_recall" ON "public"."episodic_sightings" USING "btree" ("entity_key", "concluded_at" DESC, "id");


--
-- Name: idx_episodic_verdicts_investigation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_verdicts_investigation" ON "public"."episodic_verdicts" USING "btree" ("investigation_kind", "investigation_id");


--
-- Name: idx_episodic_verdicts_recall; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_verdicts_recall" ON "public"."episodic_verdicts" USING "btree" ("concluded_at" DESC, "id");


--
-- Name: idx_episodic_verdicts_subjects; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_verdicts_subjects" ON "public"."episodic_verdicts" USING "gin" ("subject_entities");


--
-- Name: idx_episodic_verdicts_techniques; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_episodic_verdicts_techniques" ON "public"."episodic_verdicts" USING "gin" ("techniques");


--
-- Name: idx_federation_sources_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_federation_sources_enabled" ON "public"."federation_sources" USING "btree" ("enabled");


--
-- Name: idx_finding_anomaly_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_anomaly_score" ON "public"."findings" USING "btree" ("anomaly_score");


--
-- Name: idx_finding_cluster_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_cluster_id" ON "public"."findings" USING "btree" ("cluster_id");


--
-- Name: idx_finding_data_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_data_source" ON "public"."findings" USING "btree" ("data_source");


--
-- Name: idx_finding_description; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_description" ON "public"."findings" USING "gin" ("description" "public"."gin_trgm_ops");


--
-- Name: idx_finding_mitre_predictions_technique_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_mitre_predictions_technique_confidence" ON "public"."finding_mitre_predictions" USING "btree" ("technique_id", "confidence" DESC);


--
-- Name: idx_finding_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_severity" ON "public"."findings" USING "btree" ("severity");


--
-- Name: idx_finding_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_status" ON "public"."findings" USING "btree" ("status");


--
-- Name: idx_finding_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_finding_timestamp" ON "public"."findings" USING "btree" ("timestamp");


--
-- Name: idx_intake_triggers_queued_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_intake_triggers_queued_created" ON "public"."intake_triggers" USING "btree" ("created_at") WHERE (("state")::"text" = 'queued'::"text");


--
-- Name: idx_integration_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_integration_enabled" ON "public"."integration_configs" USING "btree" ("enabled");


--
-- Name: idx_integration_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_integration_type" ON "public"."integration_configs" USING "btree" ("integration_type");


--
-- Name: idx_integration_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_integration_updated_at" ON "public"."integration_configs" USING "btree" ("updated_at");


--
-- Name: idx_inv_log_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_inv_log_event_type" ON "public"."investigation_logs" USING "btree" ("event_type");


--
-- Name: idx_inv_log_investigation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_inv_log_investigation_id" ON "public"."investigation_logs" USING "btree" ("investigation_id");


--
-- Name: idx_inv_log_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_inv_log_timestamp" ON "public"."investigation_logs" USING "btree" ("timestamp");


--
-- Name: idx_investigation_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_investigation_case_id" ON "public"."investigations" USING "btree" ("case_id");


--
-- Name: idx_investigation_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_investigation_created_at" ON "public"."investigations" USING "btree" ("created_at");


--
-- Name: idx_investigation_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_investigation_priority" ON "public"."investigations" USING "btree" ("priority");


--
-- Name: idx_investigation_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_investigation_status" ON "public"."investigations" USING "btree" ("status");


--
-- Name: idx_investigation_workflow_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_investigation_workflow_id" ON "public"."investigations" USING "btree" ("workflow_id");


--
-- Name: idx_ip_exclusions_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ip_exclusions_created_at" ON "public"."ip_exclusions" USING "btree" ("created_at" DESC);


--
-- Name: idx_llm_interaction_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_interaction_agent" ON "public"."llm_interaction_logs" USING "btree" ("agent_id");


--
-- Name: idx_llm_interaction_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_interaction_created" ON "public"."llm_interaction_logs" USING "btree" ("created_at");


--
-- Name: idx_llm_interaction_investigation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_interaction_investigation" ON "public"."llm_interaction_logs" USING "btree" ("investigation_id");


--
-- Name: idx_llm_interaction_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_interaction_session" ON "public"."llm_interaction_logs" USING "btree" ("session_id");


--
-- Name: idx_llm_interaction_vk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_interaction_vk" ON "public"."llm_interaction_logs" USING "btree" ("virtual_key_id", "created_at");


--
-- Name: idx_llm_provider_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_provider_active" ON "public"."llm_provider_configs" USING "btree" ("is_active");


--
-- Name: idx_llm_provider_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_llm_provider_type" ON "public"."llm_provider_configs" USING "btree" ("provider_type");


--
-- Name: idx_mcp_credentials_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_mcp_credentials_token_hash" ON "public"."mcp_credentials" USING "btree" ("token_hash");


--
-- Name: idx_mcp_credentials_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_mcp_credentials_user" ON "public"."mcp_credentials" USING "btree" ("user_id");


--
-- Name: idx_role_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_role_name" ON "public"."roles" USING "btree" ("name");


--
-- Name: idx_shared_ioc_investigation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_shared_ioc_investigation" ON "public"."shared_iocs" USING "btree" ("investigation_id");


--
-- Name: idx_shared_ioc_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_shared_ioc_type" ON "public"."shared_iocs" USING "btree" ("ioc_type");


--
-- Name: idx_shared_ioc_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_shared_ioc_value" ON "public"."shared_iocs" USING "btree" ("value");


--
-- Name: idx_sketch_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sketch_case_id" ON "public"."sketch_mappings" USING "btree" ("case_id");


--
-- Name: idx_sketch_finding_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sketch_finding_id" ON "public"."sketch_mappings" USING "btree" ("finding_id");


--
-- Name: idx_sketch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sketch_id" ON "public"."sketch_mappings" USING "btree" ("sketch_id");


--
-- Name: idx_sla_policy_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sla_policy_active" ON "public"."sla_policies" USING "btree" ("is_active");


--
-- Name: idx_sla_policy_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sla_policy_default" ON "public"."sla_policies" USING "btree" ("is_default");


--
-- Name: idx_sla_policy_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_sla_policy_priority" ON "public"."sla_policies" USING "btree" ("priority_level");


--
-- Name: idx_system_config_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_system_config_type" ON "public"."system_config" USING "btree" ("config_type");


--
-- Name: idx_system_config_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_system_config_updated_at" ON "public"."system_config" USING "btree" ("updated_at");


--
-- Name: idx_threat_indicators_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_threat_indicators_last_seen" ON "public"."threat_indicators" USING "btree" ("last_seen" DESC);


--
-- Name: idx_threat_indicators_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_threat_indicators_source" ON "public"."threat_indicators" USING "btree" ("source");


--
-- Name: idx_threat_indicators_type_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_threat_indicators_type_value" ON "public"."threat_indicators" USING "btree" ("indicator_type", "indicator_value");


--
-- Name: idx_threat_indicators_valid_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_threat_indicators_valid_until" ON "public"."threat_indicators" USING "btree" ("valid_until");


--
-- Name: idx_user_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_user_email" ON "public"."users" USING "btree" ("email");


--
-- Name: idx_user_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_user_is_active" ON "public"."users" USING "btree" ("is_active");


--
-- Name: idx_user_role_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_user_role_id" ON "public"."users" USING "btree" ("role_id");


--
-- Name: idx_user_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_user_username" ON "public"."users" USING "btree" ("username");


--
-- Name: idx_workflow_run_phases_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_run_phases_run_id" ON "public"."workflow_run_phases" USING "btree" ("run_id", "phase_order");


--
-- Name: idx_workflow_runs_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_runs_live" ON "public"."workflow_runs" USING "btree" ("workflow_id", "started_at" DESC) WHERE ("deleted_at" IS NULL);


--
-- Name: idx_workflow_runs_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_runs_started_at" ON "public"."workflow_runs" USING "btree" ("started_at" DESC);


--
-- Name: idx_workflow_runs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_runs_status" ON "public"."workflow_runs" USING "btree" ("status") WHERE ("status" IN ('running', 'failed'));


--
-- Name: idx_workflow_runs_triggered_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_runs_triggered_by" ON "public"."workflow_runs" USING "btree" ("triggered_by", "started_at");


--
-- Name: idx_workflow_runs_workflow_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_workflow_runs_workflow_id" ON "public"."workflow_runs" USING "btree" ("workflow_id", "started_at" DESC);


--
-- Name: ix_ai_decision_logs_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "ix_ai_decision_logs_agent_id" ON "public"."ai_decision_logs" USING "btree" ("agent_id");


--
-- Name: llm_provider_default_per_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "llm_provider_default_per_type" ON "public"."llm_provider_configs" USING "btree" ("provider_type") WHERE ("is_default" = true);


--
-- Name: uniq_findings_source_extid; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uniq_findings_source_extid" ON "public"."findings" USING "btree" ("data_source", "external_id") WHERE (("data_source" IS NOT NULL) AND ("external_id" IS NOT NULL));


--
-- Name: uniq_ip_exclusions_active_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uniq_ip_exclusions_active_ip" ON "public"."ip_exclusions" USING "btree" ("ip") WHERE ("removed_at" IS NULL);


--
-- Name: uq_approval_actions_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uq_approval_actions_idempotency_key" ON "public"."approval_actions" USING "btree" ("idempotency_key") WHERE (("idempotency_key" IS NOT NULL) AND (("status")::"text" <> 'failed'::"text"));


--
-- Name: uq_intake_triggers_queued_finding; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uq_intake_triggers_queued_finding" ON "public"."intake_triggers" USING "btree" ("finding_id") WHERE ((("state")::"text" = 'queued'::"text") AND ("finding_id" IS NOT NULL));


--
-- Name: agent_events agent_events_assign_hashes; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "agent_events_assign_hashes" BEFORE INSERT ON "public"."agent_events" FOR EACH ROW EXECUTE FUNCTION "public"."agent_events_assign_hashes"();


--
-- Name: ai_model_configs trigger_ai_model_configs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_ai_model_configs_updated_at" BEFORE UPDATE ON "public"."ai_model_configs" FOR EACH ROW EXECUTE FUNCTION "public"."update_ai_model_configs_timestamp"();


--
-- Name: custom_agents trigger_custom_agents_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_custom_agents_updated_at" BEFORE UPDATE ON "public"."custom_agents" FOR EACH ROW EXECUTE FUNCTION "public"."update_custom_agents_timestamp"();


--
-- Name: integration_configs trigger_integration_configs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_integration_configs_updated_at" BEFORE UPDATE ON "public"."integration_configs" FOR EACH ROW EXECUTE FUNCTION "public"."update_integration_configs_timestamp"();


--
-- Name: llm_provider_configs trigger_llm_provider_configs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_llm_provider_configs_updated_at" BEFORE UPDATE ON "public"."llm_provider_configs" FOR EACH ROW EXECUTE FUNCTION "public"."update_llm_provider_configs_timestamp"();


--
-- Name: system_config trigger_system_config_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_system_config_updated_at" BEFORE UPDATE ON "public"."system_config" FOR EACH ROW EXECUTE FUNCTION "public"."update_system_config_timestamp"();


--
-- Name: user_preferences trigger_user_preferences_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "trigger_user_preferences_updated_at" BEFORE UPDATE ON "public"."user_preferences" FOR EACH ROW EXECUTE FUNCTION "public"."update_user_preferences_timestamp"();


--
-- Name: roles update_roles_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "update_roles_updated_at" BEFORE UPDATE ON "public"."roles" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();


--
-- Name: users update_users_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER "update_users_updated_at" BEFORE UPDATE ON "public"."users" FOR EACH ROW EXECUTE FUNCTION "public"."update_updated_at_column"();


--
-- Name: ai_decision_logs ai_decision_logs_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_decision_logs"
    ADD CONSTRAINT "ai_decision_logs_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: ai_decision_logs ai_decision_logs_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_decision_logs"
    ADD CONSTRAINT "ai_decision_logs_finding_id_fkey" FOREIGN KEY ("finding_id") REFERENCES "public"."findings"("finding_id") ON DELETE CASCADE;


--
-- Name: ai_model_configs ai_model_configs_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."ai_model_configs"
    ADD CONSTRAINT "ai_model_configs_provider_id_fkey" FOREIGN KEY ("provider_id") REFERENCES "public"."llm_provider_configs"("provider_id") ON DELETE RESTRICT;


--
-- Name: approval_actions approval_actions_workflow_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."approval_actions"
    ADD CONSTRAINT "approval_actions_workflow_run_id_fkey" FOREIGN KEY ("workflow_run_id") REFERENCES "public"."workflow_runs"("run_id") ON DELETE SET NULL;


--
-- Name: case_attachments case_attachments_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_attachments"
    ADD CONSTRAINT "case_attachments_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_closure_info case_closure_info_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_closure_info"
    ADD CONSTRAINT "case_closure_info_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_comments case_comments_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_comments"
    ADD CONSTRAINT "case_comments_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_comments case_comments_parent_comment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_comments"
    ADD CONSTRAINT "case_comments_parent_comment_id_fkey" FOREIGN KEY ("parent_comment_id") REFERENCES "public"."case_comments"("comment_id") ON DELETE CASCADE;


--
-- Name: case_escalations case_escalations_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_escalations"
    ADD CONSTRAINT "case_escalations_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_evidence case_evidence_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_evidence"
    ADD CONSTRAINT "case_evidence_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_findings case_findings_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_findings"
    ADD CONSTRAINT "case_findings_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_findings case_findings_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_findings"
    ADD CONSTRAINT "case_findings_finding_id_fkey" FOREIGN KEY ("finding_id") REFERENCES "public"."findings"("finding_id") ON DELETE CASCADE;


--
-- Name: case_iocs case_iocs_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_iocs"
    ADD CONSTRAINT "case_iocs_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_metrics case_metrics_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_metrics"
    ADD CONSTRAINT "case_metrics_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_notifications case_notifications_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_notifications"
    ADD CONSTRAINT "case_notifications_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_relationships case_relationships_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_relationships"
    ADD CONSTRAINT "case_relationships_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_relationships case_relationships_related_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_relationships"
    ADD CONSTRAINT "case_relationships_related_case_id_fkey" FOREIGN KEY ("related_case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_slas case_slas_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_slas"
    ADD CONSTRAINT "case_slas_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_slas case_slas_sla_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_slas"
    ADD CONSTRAINT "case_slas_sla_policy_id_fkey" FOREIGN KEY ("sla_policy_id") REFERENCES "public"."sla_policies"("policy_id");


--
-- Name: case_tasks case_tasks_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_tasks"
    ADD CONSTRAINT "case_tasks_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: case_tasks case_tasks_parent_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_tasks"
    ADD CONSTRAINT "case_tasks_parent_task_id_fkey" FOREIGN KEY ("parent_task_id") REFERENCES "public"."case_tasks"("task_id") ON DELETE CASCADE;


--
-- Name: case_watchers case_watchers_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."case_watchers"
    ADD CONSTRAINT "case_watchers_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_conversation_id_fkey" FOREIGN KEY ("conversation_id") REFERENCES "public"."conversations"("id") ON DELETE CASCADE;


--
-- Name: episodic_verdict_sources episodic_verdict_sources_verdict_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."episodic_verdict_sources"
    ADD CONSTRAINT "episodic_verdict_sources_verdict_id_fkey" FOREIGN KEY ("verdict_id") REFERENCES "public"."episodic_verdicts"("id") ON DELETE CASCADE;


--
-- Name: finding_mitre_predictions finding_mitre_predictions_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."finding_mitre_predictions"
    ADD CONSTRAINT "finding_mitre_predictions_finding_id_fkey" FOREIGN KEY ("finding_id") REFERENCES "public"."findings"("finding_id") ON DELETE CASCADE;


--
-- Name: investigation_logs investigation_logs_investigation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."investigation_logs"
    ADD CONSTRAINT "investigation_logs_investigation_id_fkey" FOREIGN KEY ("investigation_id") REFERENCES "public"."investigations"("investigation_id") ON DELETE CASCADE;


--
-- Name: investigations investigations_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."investigations"
    ADD CONSTRAINT "investigations_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE SET NULL;


--
-- Name: mcp_credentials mcp_credentials_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."mcp_credentials"
    ADD CONSTRAINT "mcp_credentials_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."users"("user_id") ON DELETE CASCADE;


--
-- Name: shared_iocs shared_iocs_investigation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."shared_iocs"
    ADD CONSTRAINT "shared_iocs_investigation_id_fkey" FOREIGN KEY ("investigation_id") REFERENCES "public"."investigations"("investigation_id") ON DELETE CASCADE;


--
-- Name: sketch_mappings sketch_mappings_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sketch_mappings"
    ADD CONSTRAINT "sketch_mappings_case_id_fkey" FOREIGN KEY ("case_id") REFERENCES "public"."cases"("case_id") ON DELETE CASCADE;


--
-- Name: sketch_mappings sketch_mappings_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."sketch_mappings"
    ADD CONSTRAINT "sketch_mappings_finding_id_fkey" FOREIGN KEY ("finding_id") REFERENCES "public"."findings"("finding_id") ON DELETE CASCADE;


--
-- Name: users users_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_role_id_fkey" FOREIGN KEY ("role_id") REFERENCES "public"."roles"("role_id");


--
-- Name: workflow_run_phases workflow_run_phases_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."workflow_run_phases"
    ADD CONSTRAINT "workflow_run_phases_run_id_fkey" FOREIGN KEY ("run_id") REFERENCES "public"."workflow_runs"("run_id") ON DELETE CASCADE;


--
-- Name: SCHEMA "public"; Type: ACL; Schema: -; Owner: -
--

GRANT ALL ON SCHEMA "public" TO "vigil_app";


--
-- Name: TABLE "agent_directives"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."agent_directives" TO "vigil_app";


--
-- Name: SEQUENCE "agent_directives_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."agent_directives_id_seq" TO "vigil_app";


--
-- Name: TABLE "agent_events"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE "public"."agent_events" TO "vigil_app";


--
-- Name: TABLE "agent_run_leases"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."agent_run_leases" TO "vigil_app";


--
-- Name: TABLE "ai_decision_logs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."ai_decision_logs" TO "vigil_app";


--
-- Name: SEQUENCE "ai_decision_logs_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."ai_decision_logs_id_seq" TO "vigil_app";


--
-- Name: TABLE "ai_model_configs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."ai_model_configs" TO "vigil_app";


--
-- Name: TABLE "approval_actions"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."approval_actions" TO "vigil_app";


--
-- Name: TABLE "case_attachments"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_attachments" TO "vigil_app";


--
-- Name: SEQUENCE "case_attachments_attachment_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_attachments_attachment_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_audit_logs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_audit_logs" TO "vigil_app";


--
-- Name: SEQUENCE "case_audit_logs_audit_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_audit_logs_audit_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_closure_info"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_closure_info" TO "vigil_app";


--
-- Name: TABLE "case_comments"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_comments" TO "vigil_app";


--
-- Name: SEQUENCE "case_comments_comment_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_comments_comment_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_escalations"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_escalations" TO "vigil_app";


--
-- Name: SEQUENCE "case_escalations_escalation_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_escalations_escalation_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_evidence"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_evidence" TO "vigil_app";


--
-- Name: SEQUENCE "case_evidence_evidence_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_evidence_evidence_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_findings"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_findings" TO "vigil_app";


--
-- Name: TABLE "case_iocs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_iocs" TO "vigil_app";


--
-- Name: SEQUENCE "case_iocs_ioc_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_iocs_ioc_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_metrics"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_metrics" TO "vigil_app";


--
-- Name: TABLE "case_notifications"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_notifications" TO "vigil_app";


--
-- Name: SEQUENCE "case_notifications_notification_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_notifications_notification_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_relationships"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_relationships" TO "vigil_app";


--
-- Name: SEQUENCE "case_relationships_relationship_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_relationships_relationship_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_slas"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_slas" TO "vigil_app";


--
-- Name: SEQUENCE "case_slas_sla_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_slas_sla_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_tasks"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_tasks" TO "vigil_app";


--
-- Name: SEQUENCE "case_tasks_task_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."case_tasks_task_id_seq" TO "vigil_app";


--
-- Name: TABLE "case_templates"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_templates" TO "vigil_app";


--
-- Name: TABLE "case_watchers"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."case_watchers" TO "vigil_app";


--
-- Name: TABLE "cases"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."cases" TO "vigil_app";


--
-- Name: TABLE "chat_messages"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."chat_messages" TO "vigil_app";


--
-- Name: SEQUENCE "chat_messages_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."chat_messages_id_seq" TO "vigil_app";


--
-- Name: TABLE "config_audit_log"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."config_audit_log" TO "vigil_app";


--
-- Name: SEQUENCE "config_audit_log_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."config_audit_log_id_seq" TO "vigil_app";


--
-- Name: TABLE "conversations"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."conversations" TO "vigil_app";


--
-- Name: TABLE "custom_agents"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."custom_agents" TO "vigil_app";


--
-- Name: TABLE "custom_workflows"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."custom_workflows" TO "vigil_app";


--
-- Name: TABLE "integration_configs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."integration_configs" TO "vigil_app";


--
-- Name: TABLE "enabled_integrations"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."enabled_integrations" TO "vigil_app";


--
-- Name: TABLE "episodic_distil_failures"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_distil_failures" TO "vigil_app";


--
-- Name: TABLE "episodic_distil_markers"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_distil_markers" TO "vigil_app";


--
-- Name: TABLE "episodic_gaps"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_gaps" TO "vigil_app";


--
-- Name: SEQUENCE "episodic_gaps_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."episodic_gaps_id_seq" TO "vigil_app";


--
-- Name: TABLE "episodic_read_log"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_read_log" TO "vigil_app";


--
-- Name: SEQUENCE "episodic_read_log_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."episodic_read_log_id_seq" TO "vigil_app";


--
-- Name: TABLE "episodic_sightings"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_sightings" TO "vigil_app";


--
-- Name: SEQUENCE "episodic_sightings_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."episodic_sightings_id_seq" TO "vigil_app";


--
-- Name: TABLE "episodic_verdict_sources"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_verdict_sources" TO "vigil_app";


--
-- Name: SEQUENCE "episodic_verdict_sources_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."episodic_verdict_sources_id_seq" TO "vigil_app";


--
-- Name: TABLE "episodic_verdicts"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."episodic_verdicts" TO "vigil_app";


--
-- Name: SEQUENCE "episodic_verdicts_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."episodic_verdicts_id_seq" TO "vigil_app";


--
-- Name: TABLE "federation_sources"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."federation_sources" TO "vigil_app";


--
-- Name: TABLE "finding_mitre_predictions"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."finding_mitre_predictions" TO "vigil_app";


--
-- Name: TABLE "findings"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."findings" TO "vigil_app";


--
-- Name: TABLE "intake_triggers"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."intake_triggers" TO "vigil_app";


--
-- Name: SEQUENCE "intake_triggers_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."intake_triggers_id_seq" TO "vigil_app";


--
-- Name: TABLE "investigation_logs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."investigation_logs" TO "vigil_app";


--
-- Name: SEQUENCE "investigation_logs_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."investigation_logs_id_seq" TO "vigil_app";


--
-- Name: TABLE "investigations"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."investigations" TO "vigil_app";


--
-- Name: TABLE "ip_exclusions"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."ip_exclusions" TO "vigil_app";


--
-- Name: TABLE "llm_interaction_logs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."llm_interaction_logs" TO "vigil_app";


--
-- Name: SEQUENCE "llm_interaction_logs_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."llm_interaction_logs_id_seq" TO "vigil_app";


--
-- Name: TABLE "llm_provider_configs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."llm_provider_configs" TO "vigil_app";


--
-- Name: TABLE "mcp_credentials"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."mcp_credentials" TO "vigil_app";


--
-- Name: TABLE "recent_config_changes"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."recent_config_changes" TO "vigil_app";


--
-- Name: TABLE "roles"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."roles" TO "vigil_app";


--
-- Name: TABLE "shared_iocs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."shared_iocs" TO "vigil_app";


--
-- Name: SEQUENCE "shared_iocs_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."shared_iocs_id_seq" TO "vigil_app";


--
-- Name: TABLE "sketch_mappings"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."sketch_mappings" TO "vigil_app";


--
-- Name: SEQUENCE "sketch_mappings_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."sketch_mappings_id_seq" TO "vigil_app";


--
-- Name: TABLE "sla_policies"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."sla_policies" TO "vigil_app";


--
-- Name: TABLE "system_config"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."system_config" TO "vigil_app";


--
-- Name: TABLE "threat_indicators"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."threat_indicators" TO "vigil_app";


--
-- Name: SEQUENCE "threat_indicators_id_seq"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE "public"."threat_indicators_id_seq" TO "vigil_app";


--
-- Name: TABLE "user_preferences"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."user_preferences" TO "vigil_app";


--
-- Name: TABLE "users"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."users" TO "vigil_app";


--
-- Name: TABLE "workflow_run_phases"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."workflow_run_phases" TO "vigil_app";


--
-- Name: TABLE "workflow_runs"; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."workflow_runs" TO "vigil_app";


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE __VIGIL_OWNER__ IN SCHEMA "public" GRANT SELECT,USAGE ON SEQUENCES TO "vigil_app";


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE __VIGIL_OWNER__ IN SCHEMA "public" GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO "vigil_app";


--
-- PostgreSQL database dump complete
--


