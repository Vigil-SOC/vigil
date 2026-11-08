-- A custom agent is its operator manifest (#4024). The revision beside this file
-- fills any row still without one; the persona columns then go.

ALTER TABLE public.custom_agents
    ALTER COLUMN manifest SET NOT NULL,
    DROP COLUMN name,
    DROP COLUMN description,
    DROP COLUMN icon,
    DROP COLUMN color,
    DROP COLUMN specialization,
    DROP COLUMN role,
    DROP COLUMN extra_principles,
    DROP COLUMN methodology,
    DROP COLUMN system_prompt_override,
    DROP COLUMN recommended_tools,
    DROP COLUMN max_tokens,
    DROP COLUMN enable_thinking,
    DROP COLUMN model,
    DROP COLUMN fallback_model,
    DROP COLUMN component_category;
