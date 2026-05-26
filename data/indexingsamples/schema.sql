--
-- PostgreSQL database dump
--

\restrict CDxHVeH2DzPaPdYEzXbVJIism0gyXFUOKLvuNxBmGbkpe0DSa0o1aNyPhmPRblb

-- Dumped from database version 17.5
-- Dumped by pg_dump version 17.5

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: curriculum_structure_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.curriculum_structure_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    curriculum_object_id uuid NOT NULL,
    days_to_complete integer,
    display_seq integer,
    due_date timestamp with time zone,
    due_date_type_id integer,
    is_auto_launch boolean,
    is_auto_register boolean,
    is_pay_upfront boolean,
    is_preapproved boolean,
    is_reassign_curriculum boolean,
    max_attempts integer,
    object_id uuid NOT NULL,
    parent_object_id uuid NOT NULL,
    relation_seq integer,
    required_training_per_section integer
);


--
-- Name: COLUMN curriculum_structure_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN curriculum_structure_core.curriculum_object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.curriculum_object_id IS 'ID of the curriculum. Use [training_local_core] reporting object to get localized title.';


--
-- Name: COLUMN curriculum_structure_core.days_to_complete; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.days_to_complete IS 'Number of days after specified event when the Relative Days criteria is selected for due date in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.display_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.display_seq IS 'Display sequence of child objects in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.due_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.due_date IS 'The due date for the training when the "Fixed Date" criteria is selected in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.due_date_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.due_date_type_id IS 'Unique identifier of the the due date criteria. Use [curriculum_due_date_type_local_core] reporting object to get localized text.';


--
-- Name: COLUMN curriculum_structure_core.is_auto_launch; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.is_auto_launch IS 'Flag indicating if "Auto-Launch" option is set for the training in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.is_auto_register; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.is_auto_register IS 'Flag indicating if "Auto-Register" option is set for the training in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.is_pay_upfront; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.is_pay_upfront IS 'Flag indicating if "Pay-Upfront" option is set for the training in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.is_preapproved; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.is_preapproved IS 'Flag indicating if "Pre-Approved" option is set for the training in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.is_reassign_curriculum; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.is_reassign_curriculum IS 'Flag indicating if "Reassign Curriculum upon Test Failure" option is set for the training in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.max_attempts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.max_attempts IS 'Maximum number of attempts for Tests defined in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.object_id IS 'Unique identifier of the Training Course Catalog Entry within the curriculum. Use [training_local_core] reporting object to get localized title.';


--
-- Name: COLUMN curriculum_structure_core.parent_object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.parent_object_id IS 'Unique identifier of the parent Training Course Catalog Entry of the Training Course Catalog Entry within the curriculum; that means this could be a curriculum or a section. Note that parent_object_id without corresponding entry in [training_core] represents a section. Use [training_local_core] to get localized title.';


--
-- Name: COLUMN curriculum_structure_core.relation_seq; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.relation_seq IS 'Sequence of the child objects in the curriculum structure.';


--
-- Name: COLUMN curriculum_structure_core.required_training_per_section; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.curriculum_structure_core.required_training_per_section IS 'Number of required training within the section.';


--
-- Name: ou_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ou_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    active boolean NOT NULL,
    approver_id integer,
    ou_id integer NOT NULL,
    owner_id integer,
    parent_id integer,
    ref character varying(200),
    title character varying(8000),
    type_id integer NOT NULL
);


--
-- Name: COLUMN ou_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN ou_core.active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.active IS 'Flag indicating whether Organizational Unit is active: 1 = True, 0 = False.';


--
-- Name: COLUMN ou_core.approver_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.approver_id IS 'Unique user identifier of the approver.';


--
-- Name: COLUMN ou_core.ou_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.ou_id IS 'Unique identifier of the Organizational Unit.';


--
-- Name: COLUMN ou_core.owner_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.owner_id IS 'Unique user identifier of the Organizational Unit owner.';


--
-- Name: COLUMN ou_core.parent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.parent_id IS 'Unique identifier of the parent Organizational Unit.';


--
-- Name: COLUMN ou_core.ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.ref IS 'Identifier of the Organizational Unit. This is "ID" field for Organizational Units in the application.';


--
-- Name: COLUMN ou_core.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.title IS 'Organizational Unit title.';


--
-- Name: COLUMN ou_core.type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ou_core.type_id IS 'Unique identifier of the Organizational Unit type.';


--
-- Name: training_assignment_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_assignment_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    adr_in_progress_training boolean,
    adr_training_within_curricula boolean,
    adra_ugrade_to_latest_version boolean,
    aes_assign_training boolean,
    aes_register_training boolean,
    aes_training_with_curricula boolean,
    apsd_fixed_dt timestamp with time zone,
    apsd_relative_custom_field_id integer,
    apsd_relative_int integer,
    apsd_relative_trigger integer,
    apsd_relative_unit character varying(2),
    assignment_id integer NOT NULL,
    assignment_new_occurence boolean,
    assignment_processing_frequency integer,
    comments character varying(4000),
    create_dt timestamp with time zone NOT NULL,
    created_by_user_id integer NOT NULL,
    due_date_custom_field_id integer,
    due_date_duration integer,
    due_date_duration_unit character varying(2),
    due_date_fixed_dt timestamp with time zone,
    due_date_type_id integer,
    email_option_id integer NOT NULL,
    is_active boolean,
    is_bypass_user_payment boolean,
    is_dynamic boolean,
    is_dynamic_reassignment boolean,
    is_dynamic_removal boolean,
    is_maintain_progress boolean,
    is_training_required boolean,
    object_id uuid NOT NULL,
    purpose_id integer,
    recurrence_annual_dt timestamp with time zone,
    recurrence_relative_int integer,
    recurrence_relative_only_if_complete boolean,
    recurrence_relative_trigger integer,
    recurrence_relative_unit character varying(2),
    recurrence_termination_fixed_dt timestamp with time zone,
    recurrence_termination_occurrence_limit integer,
    recurrence_termination_type_id integer,
    recurrence_type_id integer,
    requirement_tag_id integer,
    status_id character varying(4) NOT NULL,
    training_start_dt timestamp with time zone,
    workflow_type_id character varying(4) NOT NULL
);


--
-- Name: COLUMN training_assignment_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_assignment_core.adr_in_progress_training; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.adr_in_progress_training IS 'Flag indicating if in progress training will be removed by dynamic removal (applies only to Dynamic assignment). Values: 1 = Yes, 0 = No. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.adr_training_within_curricula; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.adr_training_within_curricula IS 'Flag indicating if in training within curricula will be removed by dynamic removal (applies only to Dynamic assignment). Values: 1 = Yes, 0 = No. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.adra_ugrade_to_latest_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.adra_ugrade_to_latest_version IS 'Flag indicating if users will be upgraded to the latest version by dynamic re-assignment (applies only to Dynamic assignment). Values: 1 = Yes, 0 = No. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.aes_assign_training; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.aes_assign_training IS 'Flag indicating if Assign Training emails will be sent to users (only if ''Training Specific Emails'' is selected). Values: 1 = Yes, 0 = No. Field has value NULL if ''Training Specific Emails'' is not selected.';


--
-- Name: COLUMN training_assignment_core.aes_register_training; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.aes_register_training IS 'Flag indicating if Register Training emails will be sent to users (only if ''Training Specific Emails'' is selected). Values: 1 = Yes, 0 = No. Field has value NULL if ''Training Specific Emails'' is not selected.';


--
-- Name: COLUMN training_assignment_core.aes_training_with_curricula; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.aes_training_with_curricula IS 'Flag indicating if emails will be sent for the training within the curriculum included in the assignment. Values: 1 = Yes, 0 = No. Field has value NULL when ''Training Type'' does not equal ''Curriculum''.';


--
-- Name: COLUMN training_assignment_core.apsd_fixed_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.apsd_fixed_dt IS 'The date the assignment will start to process and assign training to users (only if ''Fixed'' is selected). Field has value NULL when ''Assignment Processing Start Date - Type'' does not equal ''Fixed''.';


--
-- Name: COLUMN training_assignment_core.apsd_relative_custom_field_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.apsd_relative_custom_field_id IS 'The unique identifier of the custom field that has been selected for the processing start date of the assignment (only if ''Relative'' is selected). Field has value NULL when ''Assignment Processing Start Date - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.apsd_relative_int; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.apsd_relative_int IS 'The integer that has been set for the relative processing start date of the assignment (only if ''Relative'' is selected). Field has value NULL when ''Assignment Processing Start Date - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.apsd_relative_trigger; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.apsd_relative_trigger IS 'Internal identifier of the trigger that has been selected for the relative processing start date of the assignment (only if ''Relative'' is selected). Values: 1 = Relative to Hire Date, 2 = Relative to Custom Field. Field has value NULL when ''Assignment Processing Start Date - Type'' does not equal ''Relative''. Use [training_assignment_schedule_relative_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.apsd_relative_unit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.apsd_relative_unit IS 'The unit that has been selected for the relative processing start date of the assignment (only if ''Relative'' is selected). Values: ''dd'' = Days, ''mm'' = Months, ''yy'' = Years. Field has value NULL when ''Assignment Processing Start Date - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.assignment_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.assignment_id IS 'Unique identifier of the training assignment.';


--
-- Name: COLUMN training_assignment_core.assignment_new_occurence; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.assignment_new_occurence IS 'Flag indicating if the training will be assigned to users that already have it on their transcript. Values: 1 = Yes, 0 = No.';


--
-- Name: COLUMN training_assignment_core.assignment_processing_frequency; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.assignment_processing_frequency IS 'Internal Identifier for the assignment processing start date type. This is when the assignment will process to check for users that meet the user criteria. Values: 1 = Immediately (Daily), 2 = Fixed (Annually), 3 = Relative. Field has value NULL when ''Assignment Type'' does not equal ''Dynamic''. Use [training_assignment_schedule_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.comments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.comments IS 'Comments for the training assignment.';


--
-- Name: COLUMN training_assignment_core.create_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.create_dt IS 'Date the training assignment was created.';


--
-- Name: COLUMN training_assignment_core.created_by_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.created_by_user_id IS 'Unique identifier of the user who created training assignment.';


--
-- Name: COLUMN training_assignment_core.due_date_custom_field_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.due_date_custom_field_id IS 'The unique identifier of the custom field that has been selected for the training due date of the assignment (only if ''Relative'' is selected, ''Assignment Training Due Date - Type'' equals ''Relative'', otherwise NULL).';


--
-- Name: COLUMN training_assignment_core.due_date_duration; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.due_date_duration IS 'The integer that has been set for the training due date of the assignment (only if ''Assignment Training Due Date - Type'' equals ''Relative'', otherwise NULL).';


--
-- Name: COLUMN training_assignment_core.due_date_duration_unit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.due_date_duration_unit IS 'The unit that has been selected for the training due date of the assignment (only if ''Assignment Training Due Date - Type'' equals ''Relative'', otherwise NULL). Values: ''dd'' = Days, ''mm'' = Months, ''yy'' = Years.';


--
-- Name: COLUMN training_assignment_core.due_date_fixed_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.due_date_fixed_dt IS 'Date that the training will be due. Note that field has value only if ''Assignment Training Due Date - Type'' equals ''Fixed'', otherwise NULL.';


--
-- Name: COLUMN training_assignment_core.due_date_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.due_date_type_id IS 'Unique identifier of the training due date type. This indicates if the training due date has been set for the assignment. If it has, then it indicates if it is relative or fixed. Values: 0 = None, 1 = Fixed, 2 = Relative to Assigned Date, 3 = Relative to Hire Date, 4 = Relative to Custom Field.';


--
-- Name: COLUMN training_assignment_core.email_option_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.email_option_id IS 'Internal identifier for the email setting that was set for the assignment. Values: 0 = Default Emails, 1 = Training Specific Emails, 2 = Custom Emails, 3 = Ad-Hoc Emails, 4 = No Emails. Use [training_assignment_email_option] reporting object to get Assignment Email Settings description.';


--
-- Name: COLUMN training_assignment_core.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_active IS 'Flag indicating whether the training assignment is active or not (applies only to Dynamic assignment). Values: 1 = Active, 0 = Inactive. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.is_bypass_user_payment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_bypass_user_payment IS 'Flag indicating if user payment will be bypassed upon registration. Values: 1 = Yes, 0 = No. Field has value NULL if when ''Assignment Training Workflow'' does not equal ''ENRL'' (Assigned, Approved, and Registered).';


--
-- Name: COLUMN training_assignment_core.is_dynamic; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_dynamic IS 'Flag indicating whether the training assignment is dynamic or not (standard assignment). This field defines the type of assignment. 1 = Dynamic, 0 = Standard.';


--
-- Name: COLUMN training_assignment_core.is_dynamic_reassignment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_dynamic_reassignment IS 'Flag indicating if dynamic re-assignment is enabled for the assignment (applies only to Dynamic assignment). Values: 1 = Yes, 0 = No. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.is_dynamic_removal; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_dynamic_removal IS 'Flag indicating if dynamic removal is enabled for the assignment (applies only to Dynamic assignment). Values: 1 = Yes, 0 = No. Field has value NULL if assignment is Standard assignment.';


--
-- Name: COLUMN training_assignment_core.is_maintain_progress; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_maintain_progress IS 'Flag indicating the status of the ''Maintain Progress'' toggle in the Learning Assignment. Values: 1 = Yes, 0 = No.';


--
-- Name: COLUMN training_assignment_core.is_training_required; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.is_training_required IS 'Flag indicating whether training is required: 1 = True, 0 = False.';


--
-- Name: COLUMN training_assignment_core.object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.object_id IS 'Unique identifier of the Training Course Catalog Entry that was selected in the training assignment.';


--
-- Name: COLUMN training_assignment_core.purpose_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.purpose_id IS 'Internal identifier of training purpose. Use [training_purpose_local2] reporting object to get localized training purpose title.';


--
-- Name: COLUMN training_assignment_core.recurrence_annual_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_annual_dt IS 'The date that the training will recur (only if ''Annually'' is selected). Field has value NULL when ''Assignment Recurrence - Type'' does not equal ''Annually''.';


--
-- Name: COLUMN training_assignment_core.recurrence_relative_int; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_relative_int IS 'The integer that has been set for the recurrence setting of the assignment (only if ''Relative'' is selected). Field has value NULL when ''Assignment Recurrence - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.recurrence_relative_only_if_complete; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_relative_only_if_complete IS 'Flag indicating if the training will recur only if the previous occurrence is completed (only if ''Relative'' is selected). Values: 1 = Yes, 0 = No. Field has value NULL when ''Assignment Recurrence - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.recurrence_relative_trigger; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_relative_trigger IS 'Internal identifier of the trigger that has been selected for the recurrence setting of the assignment (only if ''Relative'' is selected). Values: 1 = Relative to Assigned Date, 2 = Relative to Completed Date, 3 = Relative to Registered Date, 4 = Relative to Custom Field. Field has value NULL when ''Assignment Recurrence - Type'' does not equal ''Relative''. Use [training_assignment_recurrence_relative_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.recurrence_relative_unit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_relative_unit IS 'The unit that has been selected for the recurrence setting of the assignment (only if ''Relative'' is selected). Values: ''dd'' = Days, ''mm'' = Months, ''yy'' = Years. Field has value NULL when ''Assignment Recurrence - Type'' does not equal ''Relative''.';


--
-- Name: COLUMN training_assignment_core.recurrence_termination_fixed_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_termination_fixed_dt IS 'The date that the recurrence will end (only if ''Fixed'' is selected). Field has value NULL when ''Assignment Recurrence - Termination - Type'' does not equal ''Fixed''.';


--
-- Name: COLUMN training_assignment_core.recurrence_termination_occurrence_limit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_termination_occurrence_limit IS 'The number of occurrences that need to happen before the recurrence ends (only if ''Occurrence Limit'' is selected). Field has value NULL when ''Assignment Recurrence - Termination - Type'' does not equal ''Occurrence Limit''.';


--
-- Name: COLUMN training_assignment_core.recurrence_termination_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_termination_type_id IS 'Internal identifier of the termination type that indicates if recurrence termination has been set for the assignment. If it has, then it indicates if it is fixed or an occurrence limit. Values: 1 = Never, 2 = Fixed, 3 = After Number of Occurrences. Field has value NULL when ''Assignment Type'' does not equal ''Dynamic''. Use [training_assignment_termination_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.recurrence_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.recurrence_type_id IS 'Internal identifier of the recurrence type that indicates if recurrence has been set for the assignment (and if so, if the training will recur annually or relatively). Values: 1 = No, 2 = Yes (Annually), 3 = Yes (Relative). Field has value NULL when ''Assignment Type'' does not equal ''Dynamic''. Use [training_assignment_recurrence_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.requirement_tag_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.requirement_tag_id IS 'Unique identifier of the training requirement tag.';


--
-- Name: COLUMN training_assignment_core.status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.status_id IS 'Textual code for the status of the training assignment (applies to both Standard and Dynamic assignments). Values: ''ACTV'' = Active, ''CNCL'' = Cancelled, ''QUED'' = Queued, ''DONE'' = Processed. Use [training_assignment_status_type] reporting object to get descriptions.';


--
-- Name: COLUMN training_assignment_core.training_start_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.training_start_dt IS 'This is the date that the training will be available on the user''s transcript (only if Assignment Type ''Standard'' is selected). Field has value NULL when ''Assignment Type'' does not equal ''Standard''.';


--
-- Name: COLUMN training_assignment_core.workflow_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_core.workflow_type_id IS 'Textual code of the assignment training workflow. This indicates how the training included in the assignment will be assigned to users and indicates if the training was assigned by a new version or new score. Values: ''ASGN'' = Assigned only, ''RCMP'' = New Version and Maintain Progress, ''ENRL'' = Assigned, Approved, and Registered, ''APPR'' = Assigned and Approved, ''UPGD'' = New Version, ''CMPL'' = Completed, ''SCRE'' = New Score. Use [training_assignment_enroll_type] reporting object to get Assignment Training Workflow description.';


--
-- Name: training_assignment_user_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_assignment_user_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    is_dynamic_removed boolean NOT NULL,
    proxy_id integer NOT NULL,
    user_id integer NOT NULL
);


--
-- Name: COLUMN training_assignment_user_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_user_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_assignment_user_core.is_dynamic_removed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_user_core.is_dynamic_removed IS 'Flag indicating if the specific user is currently dynamically removed from the assignment (values: 0/1).';


--
-- Name: COLUMN training_assignment_user_core.proxy_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_user_core.proxy_id IS 'Unique identifier of the training assignment proxy.';


--
-- Name: COLUMN training_assignment_user_core.user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_assignment_user_core.user_id IS 'Unique identifier of the user.';


--
-- Name: training_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    browser_compatibility_mode character varying(20),
    classification_id uuid,
    course_code character varying(108),
    deactivation_dt timestamp with time zone,
    effective_eval_01_id integer,
    effective_eval_02_id integer,
    effective_eval_03_id integer,
    email_option_id integer,
    event_max_enrollment integer,
    event_min_enrollment integer,
    gcid character varying(120),
    is_available_offline boolean,
    is_available_offline_network boolean,
    is_excluded_from_recommendations boolean NOT NULL,
    is_latest_training_version boolean,
    is_multi_assignment_allowed boolean NOT NULL,
    is_multi_request_allowed boolean NOT NULL,
    is_multi_request_orig_approval_applied boolean NOT NULL,
    lo_active character varying(1),
    lo_admin_session_selection_allowed boolean,
    lo_adv_reg_deadline timestamp with time zone,
    lo_billing_entity integer,
    lo_connect_item_type_id integer,
    lo_contact character varying(804),
    lo_contact_user_ref character varying(800),
    lo_create_dt timestamp with time zone,
    lo_created_by_user_id integer,
    lo_credit numeric(9,2),
    lo_currency_id integer NOT NULL,
    lo_end_dt timestamp with time zone,
    lo_end_registration timestamp with time zone,
    lo_eval_01_override integer,
    lo_eval_02_override integer,
    lo_eval_03_override integer,
    lo_hours numeric(9,2),
    lo_instructor_id uuid,
    lo_interest_tracking_allowed boolean,
    lo_is_mobile_compatible boolean,
    lo_is_part_of_curriculum boolean,
    lo_language_id integer NOT NULL,
    lo_location_id integer,
    lo_locator integer,
    lo_mastery_score integer,
    lo_material_type_id integer,
    lo_max_score integer,
    lo_min_parts integer,
    lo_modified_by_user_id integer,
    lo_modified_dt timestamp with time zone,
    lo_multiple_sessions_allowed boolean,
    lo_no_show numeric(19,4),
    lo_object_type character varying(4) NOT NULL,
    lo_owner_names character varying(1600),
    lo_price numeric(19,4) NOT NULL,
    lo_product_code character varying(40),
    lo_provider_id uuid NOT NULL,
    lo_publication_id integer,
    lo_reg_max bigint,
    lo_reg_min integer,
    lo_seats_available bigint,
    lo_seats_taken bigint,
    lo_seats_total bigint,
    lo_secondary_training_provider_id uuid,
    lo_session_selection_allowed boolean,
    lo_size numeric(9,2),
    lo_start_dt timestamp with time zone,
    lo_status_type character varying(4) NOT NULL,
    lo_test_attempts_allowed integer,
    lo_test_graders_ids character varying(1600),
    lo_test_max_entries integer,
    lo_test_max_time_allowed integer,
    lo_timezone_id integer,
    lo_total_cost numeric(19,4),
    lo_total_users_requests integer,
    lo_version character varying(100),
    lo_waitlist_allowed boolean,
    lo_waitlist_auto_manage boolean,
    lo_waitlist_auto_register boolean,
    lo_withdraw_dt timestamp with time zone,
    lo_withdrawal_penalty numeric(19,4),
    object_id uuid NOT NULL,
    online_course_protocol_id character varying(4),
    proficiency_level numeric(4,1),
    provider_ref character varying(510),
    publication_create_dt timestamp with time zone,
    publication_created_by_user_id integer,
    ref character varying(200),
    related_lo_id uuid,
    source_object_id uuid,
    thumbnail_location character varying(1000),
    total_sco integer,
    training_version_effective_dt timestamp with time zone,
    training_version_end_dt timestamp with time zone,
    training_version_start_dt timestamp with time zone
);


--
-- Name: COLUMN training_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_core.browser_compatibility_mode; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.browser_compatibility_mode IS 'The compatibility mode string passed through the meta tag for Internet Explorer browsers.';


--
-- Name: COLUMN training_core.classification_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.classification_id IS 'Unique identifier of the educational objective classification associated with training.';


--
-- Name: COLUMN training_core.course_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.course_code IS 'Course code.';


--
-- Name: COLUMN training_core.deactivation_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.deactivation_dt IS 'Deactivation date of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.effective_eval_01_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.effective_eval_01_id IS 'Unique identifier of the effective level 1 evaluation for the default course language.';


--
-- Name: COLUMN training_core.effective_eval_02_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.effective_eval_02_id IS 'Unique identifier of the effective level 2 evaluation for the default course language.';


--
-- Name: COLUMN training_core.effective_eval_03_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.effective_eval_03_id IS 'Unique identifier of the effective level 3 evaluation for the default course language.';


--
-- Name: COLUMN training_core.email_option_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.email_option_id IS 'Unique identifier of the Email Configuration option selected for the Training Course Catalog Entry. Possible values: 0 = System Defaults, 1 = Custom Emails, 2 = No Emails.';


--
-- Name: COLUMN training_core.event_max_enrollment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.event_max_enrollment IS 'The maximum enrollment for the Session defaults of an Event.';


--
-- Name: COLUMN training_core.event_min_enrollment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.event_min_enrollment IS 'The minimum enrollment for the Session defaults of an Event.';


--
-- Name: COLUMN training_core.gcid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.gcid IS 'GCID - Unique Course ID for courses loaded by Content Integrations.';


--
-- Name: COLUMN training_core.is_available_offline; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_available_offline IS 'Flag from the Course Catalog indicating if the online course can be downloaded for offline consumption: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_available_offline_network; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_available_offline_network IS 'Flag from the Course Catalog indicating if the online course can be launched from offline network location: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_excluded_from_recommendations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_excluded_from_recommendations IS 'Flag indicating whether training is excluded from Course Recommendations. Values: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_latest_training_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_latest_training_version IS 'Flag indicating whether the training is the latest version from the Course Catalog: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_multi_assignment_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_multi_assignment_allowed IS 'Flag indicating if Recurrence setting "Allow this training to be assigned to the same user more than once" is set: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_multi_request_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_multi_request_allowed IS 'Flag indicating if Recurrence setting "Allow users to request this training more than once" is set: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.is_multi_request_orig_approval_applied; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.is_multi_request_orig_approval_applied IS 'Flag indicating if Recurrence setting "Allow subsequent instances of training to be approved based on original approval" is set: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_active IS 'Flag indicating whether the training is active: Y = active, N = inactive.';


--
-- Name: COLUMN training_core.lo_admin_session_selection_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_admin_session_selection_allowed IS 'Flag indicating whether Admins and Managers are allowed to select an event session on behalf of a user via the Select Session option on the user''s Universal Profile - Transcript or Training Details page: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_adv_reg_deadline; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_adv_reg_deadline IS 'Deadline of the advance registration for the training.';


--
-- Name: COLUMN training_core.lo_billing_entity; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_billing_entity IS 'Unique identifier of the billing entity.';


--
-- Name: COLUMN training_core.lo_connect_item_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_connect_item_type_id IS 'Unique identifier of the connect posting type. Use [COMPATIBILITY_connect_item_type_local] reporting object to get localized title.';


--
-- Name: COLUMN training_core.lo_contact; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_contact IS 'Semicolon delimited list of Full names of users who can be contacted regarding training.';


--
-- Name: COLUMN training_core.lo_contact_user_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_contact_user_ref IS 'Semicolon delimited list of "User ID"s of users who can be contacted regarding training.';


--
-- Name: COLUMN training_core.lo_create_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_create_dt IS 'Date the training was created.';


--
-- Name: COLUMN training_core.lo_created_by_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_created_by_user_id IS 'Unique identifier of the user who created training.';


--
-- Name: COLUMN training_core.lo_credit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_credit IS 'Credit user would receive from attending training.';


--
-- Name: COLUMN training_core.lo_currency_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_currency_id IS 'Unique identifier of the training currency.';


--
-- Name: COLUMN training_core.lo_end_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_end_dt IS 'Finish date of training (relevant for session, external training, and cohort).';


--
-- Name: COLUMN training_core.lo_end_registration; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_end_registration IS 'The latest date users can register for the training.';


--
-- Name: COLUMN training_core.lo_eval_01_override; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_eval_01_override IS 'Unique identifier of the level 1 evaluation associated with training.';


--
-- Name: COLUMN training_core.lo_eval_02_override; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_eval_02_override IS 'Unique identifier of the level 2 evaluation associated with training.';


--
-- Name: COLUMN training_core.lo_eval_03_override; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_eval_03_override IS 'Unique identifier of the level 3 evaluation associated with training.';


--
-- Name: COLUMN training_core.lo_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_hours IS 'Total duration (in hours) of the training. For instructor-led training sessions, break times are excluded.';


--
-- Name: COLUMN training_core.lo_instructor_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_instructor_id IS 'Unique identifier of the training instructor (relevant for ILT session).';


--
-- Name: COLUMN training_core.lo_interest_tracking_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_interest_tracking_allowed IS 'Flag indicating whether the option of adding the training to the user interest tracking is enabled: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_is_mobile_compatible; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_is_mobile_compatible IS 'Flag indicating whether a training has been marked as compatible with the Cornerstone mobile applications in the Course Catalog: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_is_part_of_curriculum; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_is_part_of_curriculum IS 'Flag indicating whether the training is part of curriculum: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_language_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_language_id IS 'Unique identifier of the language associated with the training.';


--
-- Name: COLUMN training_core.lo_location_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_location_id IS 'Unique identifier of the location for training to take place (relevant for ILT session).';


--
-- Name: COLUMN training_core.lo_locator; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_locator IS 'Numeric identifier for training session.';


--
-- Name: COLUMN training_core.lo_mastery_score; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_mastery_score IS 'The minimum score a user would need to get in order to pass the training.';


--
-- Name: COLUMN training_core.lo_material_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_material_type_id IS 'Unique identifier of the material type of the training (relevant for material). Use [training_material_type_local_core] reporting object to get localized title.';


--
-- Name: COLUMN training_core.lo_max_score; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_max_score IS 'Maximum score user can achieve while participating in the training.';


--
-- Name: COLUMN training_core.lo_min_parts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_min_parts IS 'Minimum parts of the training item user must participate in.';


--
-- Name: COLUMN training_core.lo_modified_by_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_modified_by_user_id IS 'Unique identifier of the user who modified training.';


--
-- Name: COLUMN training_core.lo_modified_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_modified_dt IS 'Date when training was last modified.';


--
-- Name: COLUMN training_core.lo_multiple_sessions_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_multiple_sessions_allowed IS 'Flag indicating whether the option "Allow Users To Attend Multiple Sessions" is enabled and users can register for more than one session of the event: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_no_show; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_no_show IS 'Monetary penalty for not showing up on the registered training.';


--
-- Name: COLUMN training_core.lo_object_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_object_type IS 'Type of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.lo_owner_names; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_owner_names IS 'Semicolon delimited list of full names of the training owners (relevant for Curriculum).';


--
-- Name: COLUMN training_core.lo_price; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_price IS 'The price associated with the training (used for billing purposes).';


--
-- Name: COLUMN training_core.lo_product_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_product_code IS 'Product code for the training.';


--
-- Name: COLUMN training_core.lo_provider_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_provider_id IS 'Unique identifier of the ITL session provider.';


--
-- Name: COLUMN training_core.lo_publication_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_publication_id IS 'Unique identifier of the publication the training was associated with (relevant for online courses). Use [training_publication_local] reporting object to get localized title.';


--
-- Name: COLUMN training_core.lo_reg_max; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_reg_max IS 'Maximum number of users who can register for training.';


--
-- Name: COLUMN training_core.lo_reg_min; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_reg_min IS 'Minimum number of users needed to register for training in order to launch training.';


--
-- Name: COLUMN training_core.lo_seats_available; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_seats_available IS 'Total seats available for the training.';


--
-- Name: COLUMN training_core.lo_seats_taken; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_seats_taken IS 'Total number of seats that are used by participants for the training (session includes the session total used seats, whereas event summarizes all its sessions taken seats).';


--
-- Name: COLUMN training_core.lo_seats_total; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_seats_total IS 'Total number of seats available for the training (session includes the session total capacity, whereas event summarizes all its sessions capacity).';


--
-- Name: COLUMN training_core.lo_secondary_training_provider_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_secondary_training_provider_id IS 'Unique identifier of the secondary training provider.';


--
-- Name: COLUMN training_core.lo_session_selection_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_session_selection_allowed IS 'Flag indicating whether user is allowed to select the different event sessions: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_size; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_size IS 'Training file size (in MB).';


--
-- Name: COLUMN training_core.lo_start_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_start_dt IS 'Start date of training (relevant for session, external training, and cohort).';


--
-- Name: COLUMN training_core.lo_status_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_status_type IS 'Status of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.lo_test_attempts_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_test_attempts_allowed IS 'Maximum number of attempts allowed for a test.';


--
-- Name: COLUMN training_core.lo_test_graders_ids; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_test_graders_ids IS 'Comma separated list of "User ID"s of the users who can grade the test.';


--
-- Name: COLUMN training_core.lo_test_max_entries; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_test_max_entries IS 'Maximum number of entries allowed per test per user.';


--
-- Name: COLUMN training_core.lo_test_max_time_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_test_max_time_allowed IS 'Maximum time (in minutes) user can spend on a test.';


--
-- Name: COLUMN training_core.lo_timezone_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_timezone_id IS 'Unique identifier of the time zone (relevant for ILT sessions).';


--
-- Name: COLUMN training_core.lo_total_cost; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_total_cost IS 'Total cost of training session.';


--
-- Name: COLUMN training_core.lo_total_users_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_total_users_requests IS 'Total number of users who explicitly requested training.';


--
-- Name: COLUMN training_core.lo_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_version IS 'Version of the training.';


--
-- Name: COLUMN training_core.lo_waitlist_allowed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_waitlist_allowed IS 'Flag indicating whether user can be added to the waitlist when he registers for a session with no seats available: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_waitlist_auto_manage; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_waitlist_auto_manage IS 'Flag indicating whether the system is allowed to manage the waitlist by maintaining a list of waitlisted users and granting waitlisted users a seat automatically if one becomes available: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_waitlist_auto_register; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_waitlist_auto_register IS 'Flag indicating whether user is automatically registered  for the training once he is granted a seat from the waitlist: 1 = True, 0 = False.';


--
-- Name: COLUMN training_core.lo_withdraw_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_withdraw_dt IS 'Latest date user can withdraw from the training.';


--
-- Name: COLUMN training_core.lo_withdrawal_penalty; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.lo_withdrawal_penalty IS 'Monetary penalty for withdrawal from the training after its withdrawal date.';


--
-- Name: COLUMN training_core.object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.object_id IS 'Unique identifier of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.online_course_protocol_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.online_course_protocol_id IS 'Unique identifier of the Online Course Protocol.';


--
-- Name: COLUMN training_core.proficiency_level; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.proficiency_level IS 'The numeric percentage score applied to a Training Course Catalog Entry when a Proficiency Level is assigned. Corresponding Proficiency text values can be derived by cross-referencing the ProficiencyLevel and ProficiencyLevel_local objects.';


--
-- Name: COLUMN training_core.provider_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.provider_ref IS 'Reference ID of the provider for the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.publication_create_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.publication_create_dt IS 'Date the course publication was created.';


--
-- Name: COLUMN training_core.publication_created_by_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.publication_created_by_user_id IS 'Unique identifier of the user who created the course publication.';


--
-- Name: COLUMN training_core.ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.ref IS 'Reference ID of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.related_lo_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.related_lo_id IS 'Unique identifier of the Training Course Catalog Entry which is original version of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_core.source_object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.source_object_id IS 'Source Course Catalog Entry. For Sessions, this would point to the Event. For Cohorts, this would point to the Program.';


--
-- Name: COLUMN training_core.thumbnail_location; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.thumbnail_location IS 'URL or file location for thumbnail images, used to support multi-application integration efforts.  For non-URL values, URL can be derived by using https://<portalname>[-stage|-pilot].csod.com/clientimg/<portalname>/LoThumbnail_Upload/<thumbnail_location>.';


--
-- Name: COLUMN training_core.total_sco; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.total_sco IS 'Number of modules (SCO - Quick Course) an online course consists of.';


--
-- Name: COLUMN training_core.training_version_effective_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.training_version_effective_dt IS 'Effective date of training version.';


--
-- Name: COLUMN training_core.training_version_end_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.training_version_end_dt IS 'End date of training version.';


--
-- Name: COLUMN training_core.training_version_start_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_core.training_version_start_dt IS 'Start date of training version.';


--
-- Name: training_local_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_local_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    culture_id integer NOT NULL,
    descr character varying(16384),
    is_default boolean NOT NULL,
    keywords character varying(4000),
    object_id uuid NOT NULL,
    title character varying(2000)
);


--
-- Name: COLUMN training_local_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_local_core.culture_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.culture_id IS 'Unique identifier of the culture.';


--
-- Name: COLUMN training_local_core.descr; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.descr IS 'Training description. Note that source data on rare occasions can be arbitrarily long and anything over MaxLength limit defined in the data contract will be truncated.';


--
-- Name: COLUMN training_local_core.is_default; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.is_default IS 'Flag indicating whether the culture is the default one: 1 = True, 0 = False.';


--
-- Name: COLUMN training_local_core.keywords; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.keywords IS 'Training keywords.';


--
-- Name: COLUMN training_local_core.object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.object_id IS 'Unique identifier of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_local_core.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_local_core.title IS 'Training title.';


--
-- Name: training_type_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_type_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    approval_process boolean NOT NULL,
    description character varying(500),
    object_type character varying(4) NOT NULL,
    object_type_id integer NOT NULL,
    register_upon_approval boolean NOT NULL
);


--
-- Name: COLUMN training_type_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_type_core.approval_process; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core.approval_process IS 'Flag indicating whether users need to be approved once requested this training type: 1 = True, 0 = False.';


--
-- Name: COLUMN training_type_core.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core.description IS 'Description of the Training Course Catalog Entry type.';


--
-- Name: COLUMN training_type_core.object_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core.object_type IS 'Type of the Training Course Catalog Entry.';


--
-- Name: COLUMN training_type_core.object_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core.object_type_id IS 'Unique identifier of the Training Course Catalog Entry type.';


--
-- Name: COLUMN training_type_core.register_upon_approval; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_core.register_upon_approval IS 'Flag indicating whether user status would change to register once he has been approved for training or his status would remain approved and user would need to register for training (values: 0/1): 1 = automatically registered upon approval.';


--
-- Name: training_type_local_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.training_type_local_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    culture_id integer NOT NULL,
    description character varying(500),
    is_default boolean NOT NULL,
    object_type character varying(4) NOT NULL
);


--
-- Name: COLUMN training_type_local_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_local_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN training_type_local_core.culture_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_local_core.culture_id IS 'ID of the culture.';


--
-- Name: COLUMN training_type_local_core.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_local_core.description IS 'Localized description of the Training Course Catalog Entry type.';


--
-- Name: COLUMN training_type_local_core.is_default; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_local_core.is_default IS 'Flag indicating whether the culture is the default one: 1 = True, 0 = False.';


--
-- Name: COLUMN training_type_local_core.object_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.training_type_local_core.object_type IS 'Type of the Training Course Catalog Entry.';


--
-- Name: transcript_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transcript_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    approver_exempt_comment character varying(8000),
    completed_sco integer NOT NULL,
    exempt_approver_reason_id integer,
    exempt_comment character varying(200),
    exempt_dt timestamp with time zone,
    exempt_reason_id integer,
    exemptor_id integer,
    is_assigned boolean,
    is_express_class boolean,
    is_hidden_in_ui boolean,
    is_latest_reg_num boolean NOT NULL,
    is_latest_version_on_transcript boolean NOT NULL,
    is_removed boolean,
    is_required boolean,
    is_standalone boolean,
    is_suggested boolean,
    license_expiration_dt timestamp with time zone,
    license_id integer,
    license_status_id integer,
    reg_num integer NOT NULL,
    training_purpose_category_id integer,
    training_purpose_id integer,
    transc_object_id uuid NOT NULL,
    transc_user_id integer NOT NULL,
    transcript_badge_id integer,
    transcript_badge_points integer,
    transcript_training_points integer,
    user_lo_assigned_comments character varying(500),
    user_lo_assigned_dt timestamp with time zone,
    user_lo_assignor_id integer,
    user_lo_available_dt timestamp with time zone,
    user_lo_cancellation_reason character varying(600),
    user_lo_cancellation_reason_id integer,
    user_lo_comment character varying(250),
    user_lo_comp_dt timestamp with time zone,
    user_lo_create_dt timestamp with time zone,
    user_lo_delivery_method_id integer,
    user_lo_equivalency_type integer,
    user_lo_equivalent_object_id uuid,
    user_lo_from_training_plan smallint NOT NULL,
    user_lo_last_access_dt timestamp with time zone,
    user_lo_last_action_dt timestamp with time zone,
    user_lo_last_modified_dt timestamp with time zone,
    user_lo_min_due_date timestamp with time zone,
    user_lo_minutes_participated integer,
    user_lo_pass boolean,
    user_lo_pct_complete numeric(9,2),
    user_lo_reg_dt timestamp with time zone,
    user_lo_removed_comments character varying(500),
    user_lo_removed_dt timestamp with time zone,
    user_lo_removed_reason_id integer,
    user_lo_remover_id integer,
    user_lo_score integer,
    user_lo_start_dt timestamp with time zone,
    user_lo_status_id bigint,
    user_lo_training_link_expiration_date timestamp with time zone,
    user_lo_withdrawal_date timestamp with time zone,
    user_lo_withdrawal_reason character varying(600),
    user_lo_withdrawal_reason_id integer
);


--
-- Name: COLUMN transcript_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN transcript_core.approver_exempt_comment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.approver_exempt_comment IS 'Exemption approver comments.';


--
-- Name: COLUMN transcript_core.completed_sco; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.completed_sco IS 'Number of completed modules inside an online course.';


--
-- Name: COLUMN transcript_core.exempt_approver_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.exempt_approver_reason_id IS 'Unique identifier of the approver reason to approve the exemption. Use [training_exemption_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.exempt_comment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.exempt_comment IS 'Comments noted by exemptor during exemption submission.';


--
-- Name: COLUMN transcript_core.exempt_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.exempt_dt IS 'Date of transcript training exemption.';


--
-- Name: COLUMN transcript_core.exempt_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.exempt_reason_id IS 'Unique identifier of the reason to exempt the training by the exemptor. Use [training_exemption_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.exemptor_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.exemptor_id IS 'Unique user identifier of the exemptor.';


--
-- Name: COLUMN transcript_core.is_assigned; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_assigned IS 'Flag indicating if training was assigned to the user or requested by the user.';


--
-- Name: COLUMN transcript_core.is_express_class; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_express_class IS 'Flag indicating if the specific training was assigned via Express Class: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.is_hidden_in_ui; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_hidden_in_ui IS 'Flag indicating if transcript entry is hidden from the UI via the "archive" feature: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.is_latest_reg_num; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_latest_reg_num IS 'Flag indicating if user''s registration for training is latest or not: 1 =  latest, 0 = not latest.';


--
-- Name: COLUMN transcript_core.is_latest_version_on_transcript; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_latest_version_on_transcript IS 'Flag indicating if the specific training is the latest/greatest registration on the user''s transcript across all versions: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.is_removed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_removed IS 'Flag indicating if training is removed from user transcript: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.is_required; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_required IS 'Flag indicating if training is mandatory for the user: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.is_standalone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_standalone IS 'Flag indicating if a training on a user''s transcript was registered as a standalone course and not as part of a curriculum.';


--
-- Name: COLUMN transcript_core.is_suggested; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.is_suggested IS 'Flag indicating if training was suggested to the user: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.license_expiration_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.license_expiration_dt IS 'User license expiration date. If expiration date set, this is coming from the expiration of license set ([expiration_dt] field in [training_license_core] object).';


--
-- Name: COLUMN transcript_core.license_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.license_id IS 'Unique identifier of the license.';


--
-- Name: COLUMN transcript_core.license_status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.license_status_id IS 'Unique identifier of the license status. License can have the following statuses: renewal (allows you to renew), expired.';


--
-- Name: COLUMN transcript_core.reg_num; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.reg_num IS 'The number of times the user registered for the training.';


--
-- Name: COLUMN transcript_core.training_purpose_category_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.training_purpose_category_id IS 'Unique identifier of the training purpose category. Use [training_purpose_local2] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.training_purpose_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.training_purpose_id IS 'Unique identifier of the purpose that user requested the training for. Use [training_purpose_local2] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.transc_object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.transc_object_id IS 'Unique identifier of the Training Course Catalog Entry in the transcript.';


--
-- Name: COLUMN transcript_core.transc_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.transc_user_id IS 'Unique identifier of the user.';


--
-- Name: COLUMN transcript_core.transcript_badge_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.transcript_badge_id IS 'Unique identifier of the training badge that was awarded to the user for completing the training. Use [feedback_badge_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.transcript_badge_points; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.transcript_badge_points IS 'The point value of the badge that was awarded to the user for completing the training.';


--
-- Name: COLUMN transcript_core.transcript_training_points; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.transcript_training_points IS 'The point value that was awarded to the user for completing the training.';


--
-- Name: COLUMN transcript_core.user_lo_assigned_comments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_assigned_comments IS 'Comments noted when transcript training item was assigned to the user.';


--
-- Name: COLUMN transcript_core.user_lo_assigned_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_assigned_dt IS 'Date the comments were noted when transcript training item was assigned to the user.';


--
-- Name: COLUMN transcript_core.user_lo_assignor_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_assignor_id IS 'Unique user identifier of the transcript training item assignor. Note that field has value -1 when there is no assignor (training is job requirement).';


--
-- Name: COLUMN transcript_core.user_lo_available_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_available_dt IS 'Date when the training will be available for registering.';


--
-- Name: COLUMN transcript_core.user_lo_cancellation_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_cancellation_reason IS 'Comments for the reason the administrator canceled the user participation in the training.';


--
-- Name: COLUMN transcript_core.user_lo_cancellation_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_cancellation_reason_id IS 'Unique identifier of the reason the administrator canceled the user participation in the training. Use [transcript_action_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.user_lo_comment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_comment IS 'Comments noted when transcript training item is edited.';


--
-- Name: COLUMN transcript_core.user_lo_comp_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_comp_dt IS 'Transcript training item completion date.';


--
-- Name: COLUMN transcript_core.user_lo_create_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_create_dt IS 'Transcript training item create date.';


--
-- Name: COLUMN transcript_core.user_lo_delivery_method_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_delivery_method_id IS 'Unique identifier of the Transcript Delivery Method. The Transcript Delivery Method field represents the method by which a training is delivered to a user''s transcript.

Use [transcript_delivery_method_local] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.user_lo_equivalency_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_equivalency_type IS 'ID of the type of equivalency that connects the training in ''Completed Equivalent'' status with the training in ''Completed'' status that triggered the ''Completed Equivalent'' status. Use [COMPATIBILITY_transcript_equivalency_type_local] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.user_lo_equivalent_object_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_equivalent_object_id IS 'Unique identifier of the Training Course Catalog Entry of the ''Completed'' training that triggered the ''Completed Equivalent'' status of another training. Use [training_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.user_lo_from_training_plan; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_from_training_plan IS 'Flag indicating whether the user took training as a part of training plan (values:  Yes/No/Replaced).';


--
-- Name: COLUMN transcript_core.user_lo_last_access_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_last_access_dt IS 'Transcript training item last access date.';


--
-- Name: COLUMN transcript_core.user_lo_last_action_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_last_action_dt IS 'Date when last action was taken on the user training item. Note: This field will have the date for any action that excludes archival and un-archival. Any action besides these occurring would be a candidate for setting this date.';


--
-- Name: COLUMN transcript_core.user_lo_last_modified_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_last_modified_dt IS 'The latest date the transcript has been updated via the UI or the DLW for transcript registrations, assignments or completions.';


--
-- Name: COLUMN transcript_core.user_lo_min_due_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_min_due_date IS 'Transcript training item due date.';


--
-- Name: COLUMN transcript_core.user_lo_minutes_participated; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_minutes_participated IS 'Transcript time in training (in minutes).';


--
-- Name: COLUMN transcript_core.user_lo_pass; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_pass IS 'Flag indicating whether user passed training course: 1 = True, 0 = False.';


--
-- Name: COLUMN transcript_core.user_lo_pct_complete; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_pct_complete IS 'Percentage of the training completed by the user.';


--
-- Name: COLUMN transcript_core.user_lo_reg_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_reg_dt IS 'Transcript training item registration date.';


--
-- Name: COLUMN transcript_core.user_lo_removed_comments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_removed_comments IS 'Comments noted by remover when transcript training item is removed.';


--
-- Name: COLUMN transcript_core.user_lo_removed_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_removed_dt IS 'Date when training was removed from transcript. If the object was part of the curriculum and the curriculum was removed while the object remained in the transcript, then this field will generate the date of when the parent curriculum was removed.';


--
-- Name: COLUMN transcript_core.user_lo_removed_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_removed_reason_id IS 'Unique identifier of the reason why training item was removed from transcript. Use [training_removal_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN transcript_core.user_lo_remover_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_remover_id IS 'Unique user identifier of the transcript training item remover.';


--
-- Name: COLUMN transcript_core.user_lo_score; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_score IS 'Transcript training item score.';


--
-- Name: COLUMN transcript_core.user_lo_start_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_start_dt IS 'Transcript training item start date.';


--
-- Name: COLUMN transcript_core.user_lo_status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_status_id IS 'Unique identifier of the Transcript training status.';


--
-- Name: COLUMN transcript_core.user_lo_training_link_expiration_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_training_link_expiration_date IS 'Date when link to register for training will expire.';


--
-- Name: COLUMN transcript_core.user_lo_withdrawal_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_withdrawal_date IS 'The date a user withdraws from a session.';


--
-- Name: COLUMN transcript_core.user_lo_withdrawal_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_withdrawal_reason IS 'Comments for the reason the user withdrew from the training.';


--
-- Name: COLUMN transcript_core.user_lo_withdrawal_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.transcript_core.user_lo_withdrawal_reason_id IS 'Unique identifier of the reason the user withdrew from the training. Use [transcript_action_reason_local_core] reporting object to get localized title.';


--
-- Name: user_ou_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_ou_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    ou_id integer NOT NULL,
    ou_type_id integer NOT NULL,
    status_id integer,
    user_id integer NOT NULL
);


--
-- Name: COLUMN user_ou_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.user_ou_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN user_ou_core.ou_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.user_ou_core.ou_id IS 'Unique identifier of the Organizational Unit.';


--
-- Name: COLUMN user_ou_core.ou_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.user_ou_core.ou_type_id IS 'Unique identifier of the Organizational Unit type.';


--
-- Name: COLUMN user_ou_core.status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.user_ou_core.status_id IS 'Unique identifier the user status in the Organizational Unit. Use [user_ou_status_local_core] reporting object to get localized title.';


--
-- Name: COLUMN user_ou_core.user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.user_ou_core.user_id IS 'Unique identifier of the user.';


--
-- Name: users_core; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users_core (
    _last_touched_dt_utc timestamp with time zone NOT NULL,
    applicant_archived_flag boolean NOT NULL,
    user_absent boolean NOT NULL,
    user_activation_dt timestamp with time zone,
    user_address_id integer,
    user_allow_reconcile boolean NOT NULL,
    user_appr_id integer,
    user_approvals integer NOT NULL,
    user_birth_dt timestamp with time zone,
    user_category_id integer,
    user_company_no character varying(50),
    user_create_dt timestamp with time zone NOT NULL,
    user_deactivation_dt timestamp with time zone,
    user_eligible_for_rehire boolean,
    user_email character varying(256),
    user_employment_status_id integer,
    user_ethnicity character varying(200),
    user_exempt boolean,
    user_gender character varying(20),
    user_guid uuid NOT NULL,
    user_has_photo boolean,
    user_hire_dt_last timestamp with time zone,
    user_hire_dt_orig timestamp with time zone,
    user_i_mgr_id integer,
    user_id integer NOT NULL,
    user_is_rehired_employee boolean,
    user_language_id integer,
    user_last_login timestamp with time zone,
    user_leave_reason_id integer,
    user_local_system_id character varying(200),
    user_login character varying(256),
    user_mgr_id integer,
    user_modify_date timestamp with time zone,
    user_name_first character varying(400) NOT NULL,
    user_name_last character varying(400) NOT NULL,
    user_name_middle character varying(400),
    user_name_prefix character varying(20),
    user_name_suffix character varying(20),
    user_personal_email character varying(256),
    user_phone_fax character varying(100),
    user_phone_home character varying(30),
    user_phone_mobile character varying(30),
    user_phone_work character varying(30),
    user_ref character varying(200),
    user_status_id integer,
    user_sub_category_id integer,
    user_tenure_months integer,
    user_termination_dt timestamp with time zone,
    user_termination_reason_id integer,
    user_timezone_id integer,
    user_type_id integer NOT NULL
);


--
-- Name: COLUMN users_core._last_touched_dt_utc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core._last_touched_dt_utc IS 'UTC date and time when the record has been created or most recently updated in the reporting system, not the application, although the times are usually very close. Note that an update does not necessarily mean that the value has changed; it could be the same value as before the event.';


--
-- Name: COLUMN users_core.applicant_archived_flag; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.applicant_archived_flag IS 'Flag indicating whether applicant is archived: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_absent; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_absent IS 'Flag indicating whether user is on leave: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_activation_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_activation_dt IS 'This is the start date for a user''s status. It is a part of the activation period dataset.  This date, should not be used as an independent data point; it needs to be considered together with Activation End date, and User Status, along with status change audit data.';


--
-- Name: COLUMN users_core.user_address_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_address_id IS 'Unique identifier of the user address.';


--
-- Name: COLUMN users_core.user_allow_reconcile; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_allow_reconcile IS 'Flag indicating whether user can be reconciled: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_appr_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_appr_id IS 'Unique identifier of the user approver.';


--
-- Name: COLUMN users_core.user_approvals; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_approvals IS 'Number of required user approvals.';


--
-- Name: COLUMN users_core.user_birth_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_birth_dt IS 'User birth date.';


--
-- Name: COLUMN users_core.user_category_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_category_id IS 'Unique identifier of the user category. Use [user_category_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_company_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_company_no IS 'User company number.';


--
-- Name: COLUMN users_core.user_create_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_create_dt IS 'User creation date.';


--
-- Name: COLUMN users_core.user_deactivation_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_deactivation_dt IS 'This is the end date for a user''s status. It is a part of the activation period dataset.  This date, should not be used as an independent data point; it needs to be considered together with Activation Start date, and User Status, along with status change audit data.';


--
-- Name: COLUMN users_core.user_eligible_for_rehire; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_eligible_for_rehire IS 'Flag indicating whether user is eligible for rehire: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_email; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_email IS 'User email.';


--
-- Name: COLUMN users_core.user_employment_status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_employment_status_id IS 'Unique identifier of the user employment status. Use [user_employment_status_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_ethnicity; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_ethnicity IS 'User ethnicity.';


--
-- Name: COLUMN users_core.user_exempt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_exempt IS 'Flag indicating whether user is exempted from the overtime provisions: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_gender; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_gender IS 'User gender.';


--
-- Name: COLUMN users_core.user_guid; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_guid IS 'Unique identifier (GUID) of the user. This is "User GUID" report field in the reporting system.';


--
-- Name: COLUMN users_core.user_has_photo; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_has_photo IS 'Flag indicating whether user''s photo exists: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_hire_dt_last; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_hire_dt_last IS 'User last hire date, after user was re-hired.';


--
-- Name: COLUMN users_core.user_hire_dt_orig; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_hire_dt_orig IS 'User original hire date.';


--
-- Name: COLUMN users_core.user_i_mgr_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_i_mgr_id IS 'Unique identifier of the user''s indirect manager.';


--
-- Name: COLUMN users_core.user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_id IS 'Unique identifier of the user.';


--
-- Name: COLUMN users_core.user_is_rehired_employee; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_is_rehired_employee IS 'Flag indicating whether an employee was previously employed at the organization and now has been rehired: 1 = True, 0 = False.';


--
-- Name: COLUMN users_core.user_language_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_language_id IS 'Unique identifier of the user language. Use [language_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_last_login; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_last_login IS 'User last access.';


--
-- Name: COLUMN users_core.user_leave_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_leave_reason_id IS 'Unique identifier of the user leave reason. Use [user_leave_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_local_system_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_local_system_id IS 'Local System identifier. This is "Local System ID" field on the user record in the application.';


--
-- Name: COLUMN users_core.user_login; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_login IS 'User login.';


--
-- Name: COLUMN users_core.user_mgr_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_mgr_id IS 'Unique identifier of the user''s manager.';


--
-- Name: COLUMN users_core.user_modify_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_modify_date IS 'This is the date-time stamp of when the last effective change has taken place for a given user record.';


--
-- Name: COLUMN users_core.user_name_first; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_name_first IS 'User First name.';


--
-- Name: COLUMN users_core.user_name_last; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_name_last IS 'User Last name.';


--
-- Name: COLUMN users_core.user_name_middle; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_name_middle IS 'User Middle name.';


--
-- Name: COLUMN users_core.user_name_prefix; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_name_prefix IS 'User name prefix.';


--
-- Name: COLUMN users_core.user_name_suffix; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_name_suffix IS 'User name suffix.';


--
-- Name: COLUMN users_core.user_personal_email; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_personal_email IS 'User personal email address.';


--
-- Name: COLUMN users_core.user_phone_fax; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_phone_fax IS 'User fax number.';


--
-- Name: COLUMN users_core.user_phone_home; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_phone_home IS 'User home phone.';


--
-- Name: COLUMN users_core.user_phone_mobile; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_phone_mobile IS 'User mobile number.';


--
-- Name: COLUMN users_core.user_phone_work; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_phone_work IS 'User phone number.';


--
-- Name: COLUMN users_core.user_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_ref IS '"User ID" of the user. This is "User ID" field on the user record in the application.';


--
-- Name: COLUMN users_core.user_status_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_status_id IS 'Unique identifier of the user status. Use [user_status_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_sub_category_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_sub_category_id IS 'Unique identifier of the user sub-category. Use [user_sub_category_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_tenure_months; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_tenure_months IS 'User''s months of service.';


--
-- Name: COLUMN users_core.user_termination_dt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_termination_dt IS 'User termination date.';


--
-- Name: COLUMN users_core.user_termination_reason_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_termination_reason_id IS 'Unique identifier of the user termination reason. Use [user_termination_reason_local_core] reporting object to get localized title.';


--
-- Name: COLUMN users_core.user_timezone_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_timezone_id IS 'This is the user selected timezone within their profile preferences. In case, if the user has not selected a specific timezone, in their preferences, this field will be presented with a ''null'' value. This field does not provide the fallback (portal or OU defaults) values that are calculated at runtime.';


--
-- Name: COLUMN users_core.user_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users_core.user_type_id IS 'Unique identifier of the user type.';


--
-- PostgreSQL database dump complete
--

\unrestrict CDxHVeH2DzPaPdYEzXbVJIism0gyXFUOKLvuNxBmGbkpe0DSa0o1aNyPhmPRblb

