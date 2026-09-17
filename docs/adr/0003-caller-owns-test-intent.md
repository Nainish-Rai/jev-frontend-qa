# The calling agent owns test intent

The calling coding agent supplies the goal, exact test inputs, and contract assertions; the QA CLI owns browser execution, evidence collection, and verification. Exploratory runs may omit assertions but cannot claim a contract pass. Uncertain or unsupported interactions return BLOCKED with permitted evidence instead of invoking another reasoning model, avoiding duplicated planning and silent changes to expected behavior.
