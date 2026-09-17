# Frontend QA

Frontend QA checks whether a feature behaves as intended through its user interface, its backend interaction, and its resulting saved state.

## Language

**Scenario**:
A feature behavior to exercise under specified conditions, with an intended outcome.
_Avoid_: Browser goal, click script

**Run**:
One execution of a scenario against a particular application environment.
_Avoid_: Scenario, browser session

**Evidence**:
Observations collected during a run that support or contradict its intended outcome, such as a submitted request, its response, or a value observed after reloading.
_Avoid_: Agent belief, success claim

**Assertion**:
An explicit check of observed evidence against expected behavior.
_Avoid_: Navigation instruction, agent goal

**Verdict**:
The verification outcome of a run, distinct from whether browser navigation has finished.
_Avoid_: Agent DONE, success toast

**Persistence check**:
A check that a submitted change remains observable through a fresh read rather than only in the submitting page's temporary state.
_Avoid_: Save notification, optimistic update

**Contract run**:
A run evaluated against explicit expectations grounded in feature requirements; its verdict applies only to those expectations.
_Avoid_: Whole-feature certification

**Exploratory run**:
A run that investigates a goal and reports observations or suspected defects without claiming correctness against unspecified requirements.
_Avoid_: Contract run, certified pass

**Project policy**:
The permissions defining which application environments and side effects a QA run is authorized to exercise.
_Avoid_: Model permission, blanket staging approval

**Reference app**:
A controlled application with known working behavior and deliberately introduced defects used to evaluate the tester itself.
_Avoid_: Mocked successful run
