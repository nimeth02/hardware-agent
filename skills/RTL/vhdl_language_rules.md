# VHDL language rules from the IEEE 1076 LRM: the legal-code reference behind every compile / elaboration error

Applies to every edition. Load it with the design rulebook. Everything here
comes from the IEEE VHDL Language Reference Manual (1076-1993 / 2000
edition); the section number after each rule (LRM 7.2.1) says where it is
defined. The edition rulebook (vhdl_93_ or vhdl_2008_design_rules.md) and the
spec win where they differ. Items marked [93] are legal in VHDL-2008, so the
2008 rulebook overrides them.

Most generated designs fail for one of three reasons: the code is not legal
VHDL (compile error), it is legal but the sizes or ranges do not fit (the
simulation stops at run time), or it is legal but not what was meant (wrong
output). The sections below cover all three.

===============================================================
1. NAMES, WORDS AND LITERALS (LRM 13)
===============================================================

- Upper and lower case are the same: `Count` = `COUNT` (13.3.1). Pick one
  style and keep it.
- A name is letters, digits and single underscores, starts with a letter,
  does not end with `_` and has no `__` (13.3.1). `_i`, `reg_`, `a__b`,
  `1st_stage` are all illegal.
- A reserved word can NEVER be a name (13.9). The ones generated designs hit
  most: `in out inout buffer bus register select range next exit open map
  port generic block body access after alias all file group label new null
  of on others package record rem mod abs units until wait when while with
  xor xnor sla sll sra srl rol ror type subtype variable signal constant
  function procedure process return then to downto is loop library use`.
  Safe replacements: `next` -> `nxt` / `next_state`, `range` -> `rng`,
  `register` -> `reg`, `select` -> `sel`, `open` -> `is_open`, `file` -> `fil`.
  The full list also has: and architecture array assert attribute begin case
  component configuration disconnect else elsif end entity for generate guarded
  if impure inertial linkage literal nand nor not or out pure reference reject
  report severity shared transport unaffected.
- A comment is `--` to the end of the line. There is NO block comment
  `/* */` [93].
- Integer literal: `255`, `1_000`, `16#FF#`, `2#1010#`. Real literal always
  has a point: `1.0`, `2.5e3`. `1.` and `.5` are illegal (13.4). An integer
  literal may not have a minus in its exponent: `1e-3` is illegal, write
  `1.0e-3`.
- Character literal: one char in single quotes, `'0'`, `'1'`, `'Z'`.
- String literal: double quotes, must fit on ONE line, a quote inside is
  doubled `""`. Join long text with `&` (13.6).
- Bit-string literal: `B"1010"`, `O"17"`, `X"FF"`. Its length is 1, 3 or 4
  bits per digit, so `X"FF"` is exactly 8 bits and `X"0F"` is 8 bits (13.7).
  Underscores are allowed `X"FF_00"`. [93] has no `D"..."`, no sized form
  `12X"FF"`, no signed form `SX"F"`; write a plain binary string.
- A literal such as `"0101"` or `X"3"` takes its type from where it is used;
  it is legal wherever a bit-vector type is expected (std_logic_vector,
  unsigned, signed). See 5 (type conversion) for the one place it is illegal.

===============================================================
2. DESIGN UNIT LAYOUT (LRM 1, 4.3.2, 10, 11)
===============================================================

- One entity + its architecture per file; the architecture names the
  entity: `architecture rtl of NAME is`. Closing names, if written, must
  repeat the opening name exactly: `end entity NAME;` `end architecture rtl;`
  `end process p_name;` `end if;` (1.1, 1.2).
- The library/use lines at the top of a file cover that entity AND its
  architecture (they form one region, 10.1). Write them once.
- Every port, generic, signal, constant, type, function, process label,
  generate label and instance label of one entity + architecture share ONE
  name space. Two things with the same
  name there are an error (10.3). Typical clashes: a signal named like a
  port; a process labelled `stage1` plus a signal `stage1`; a label equal to
  a type name. Give labels a prefix (`p_`, `g_`, `u_`).
- Generics are constants and may size the ports:
    entity fir is
      generic ( N : natural := 8 );
      port ( din : in std_logic_vector(N-1 downto 0); ... );
  Inside the SAME interface list one item may not use another (illegal:
  `generic (A : natural; B : natural := A)` or `port (p : in std_logic_vector;
  q : in std_logic_vector(p'range))`). A port may use a generic (4.3.2.1).
- Port list: items separated by `;`, NO `;` after the last item, then `);`.
  A trailing `;` before `)` is the commonest "syntax error near )".
- Port modes (4.3.2):
    in     read only. Assigning it is an error.
    out    write only. READING IT IS AN ERROR [93] ("cannot read from out").
    inout  read and write, bidirectional (tri-state). Not for ordinary RTL.
    buffer read and write, but only ONE driver. Do not use; use the
           `_i` internal signal pattern from the edition rulebook instead.
- Every port needs a mode and a type.

===============================================================
3. WHERE EACH DECLARATION MAY STAND (LRM 1.2.1, 2.2, 4.3.1.3, 9.2)
===============================================================

- architecture declarative part (between `is` and `begin`): signal,
  constant, type, subtype, function, procedure, component, alias,
  attribute. A `variable` here is an ERROR (it would have to be `shared`
  and of a protected type, 4.3.1.3).
- process declarative part (between `process(...)` and `begin`): variable,
  constant, type, subtype, function, procedure. NEVER signal.
- function / procedure declarative part: the same as a process.
- After `begin` there are only statements, never declarations.
- generate declarative part: optional, and then `begin` is required:
    g_x : for i in 0 to 3 generate
      signal t : std_logic;        -- only legal because `begin` follows
    begin
      ...
    end generate g_x;
- Initial values and constant values are worked out ONCE when the design
  is built, before any signal has a value. They may use literals,
  constants, generics and pure functions of those; they may NOT read a
  signal or port (12.3). Illegal: `signal b : std_logic := a;`.
- Every constant needs its value (`:=`), except a deferred constant in a
  package. A `constant` is never assigned later.

===============================================================
4. TYPES (LRM 3)
===============================================================

- A SIGNAL or VARIABLE of an array type must have a fixed range:
  `signal x : std_logic_vector(7 downto 0);`. `signal x : std_logic_vector;`
  is an error (3.2.1.1). Only constants, aliases, subprogram parameters
  and entity ports/generics may be left open (the size comes from the value
  or the actual given).
- `std_logic_vector(7 downto 0)`: use `downto` for numbers so bit 7 is the
  top bit. `(0 to 7)` makes index 0 the left-most bit, which flips every
  slice and every conversion. Use ONE direction for every vector in a
  design (descending, `N-1 downto 0`).
- Integer subtypes check their range at run time: `signal c : natural;`
  stops the simulation when `c - 1` goes below 0; `integer range 0 to 15`
  stops at 16. For counters use `unsigned(W-1 downto 0)` (it wraps) or
  guard the arithmetic. INTEGER is at least -2147483647 .. 2147483647 and
  overflow is an error (3.1.2.1).
- Enumeration type for a state machine:
    type state_t is (S_IDLE, S_RUN, S_DONE);
    signal state, nxt_state : state_t;
  The literals are names too (rules of 1 apply: `S_NEXT`, not `next`).
  Reset assigns a literal: `state <= S_IDLE;`.
- Arrays of vectors (a small table or memory):
    type mem_t is array (0 to 15) of std_logic_vector(7 downto 0);
    signal mem : mem_t;                  -- element 3, bit 2:  mem(3)(2)
  A constant table is filled with an aggregate (6), every element exactly
  once. Declare the type in the architecture declarative part.
- `real`, `time`, `access`, `file`, `protected` types are NOT synthesizable:
  never in design files. `time` only in test programs.
- Use `std_logic` / `std_logic_vector` on ports. Types `bit` / `bit_vector`
  are legal VHDL but do not mix with std_logic (no implicit conversion).

===============================================================
5. EXPRESSIONS, OPERATORS AND TYPE RULES (LRM 7.1, 7.2, 7.3.4, 7.3.5)
===============================================================

Operator strength, strongest first (7.2). Equal strength groups left to
right:
    1  **   abs   not
    2  *   /   mod   rem
    3  unary + -        (only at the very START of a simple expression)
    4  binary + -   &
    5  sll srl sla sra rol ror        [93: bit/boolean vectors only]
    6  =  /=  <  <=  >  >=
    7  and or nand nor xor xnor       (weakest)

Consequences that cause real bugs:
- `not` binds tighter than `=`: `not a = b` means `(not a) = b`. Always
  write `not (a = b)` or `a /= b`.
- `+ - &` bind tighter than comparison, so `a & b = c` means `(a & b) = c`.
  Comparison binds tighter than `and`/`or`, so `a = '1' and b = '0'` is
  fine without brackets.
- The unary sign belongs to the first term only: `a * -b`, `a - -b`,
  `a + -b`, `a ** -b` are SYNTAX errors. Write `a * (-b)`, `a - (-b)`.
  `-a * b` is `-(a*b)`; `-5 rem 2` is `-(5 rem 2)` = -1.
- `and`, `or`, `xor`, `xnor` may be chained only with the SAME operator:
  `a and b and c` is fine. Mixing them needs brackets:
  `(a and b) or c` (a bare `a and b or c` is "syntax error near or").
  `nand` and `nor` may not be chained at all: `a nand b nand c` is an error.
  (7.1)
- Both operands of a binary operator must have the SAME type. There is no
  automatic conversion between integer, signed, unsigned,
  std_logic_vector, std_logic, boolean. `x + 1` is legal for unsigned/signed
  (numeric_std has `unsigned + natural`), but `slv + 1` is not.
- `signed` and `unsigned` are DIFFERENT types: `+ - * < <= = ...` between a
  signed and an unsigned value has no definition ("found '0' definitions of
  operator"). Convert one side first. An unsigned value `u` used as a
  signed number needs one extra top bit: `signed('0' & u)`. Compare the
  two sides at the same width: `resize(signed('0' & u), 13) <= to_signed(2, 13)`
  or simply compare unsigned with unsigned: `u <= to_unsigned(2, u'length)`.
- `and or xor not ...` on vectors need both vectors the SAME LENGTH, else
  the simulation stops (7.2.1). Result of `=` and relations is BOOLEAN.
- Order comparison `<` `>` on std_logic_vector compares position by
  position as text, NOT as numbers (7.2.2): `"0111" < "1000"` happens to
  work but `"10" < "011"` compares the first bits only. Convert to
  unsigned/signed first, or compare integers.
  `=` on vectors of different length is simply false (no error).
- `mod` takes the sign of the right operand, `rem` the sign of the left
  (7.2.6). `/` between integers rounds toward zero. `**` has an INTEGER
  exponent; a negative exponent works only on reals. Divide, mod and `**`
  by a non-constant are not synthesizable; use shifts and constants.
- `&` joins vectors/elements; its result type comes from where it is used.
  If both sides are literals the result type is ambiguous: assign it to a
  declared signal. Never rely on the range or direction of a `&` result;
  put it in a named signal first (7.2.4).
- `if` and `elsif` conditions, `when`, `while`, `exit when`, `next when`
  and `assert` need a BOOLEAN [93]:
      if enable = '1' then          -- correct
      if enable then                -- error: std_logic is not boolean [93]
      if a = '1' and b = '0' then   -- correct
  [2008] allows `if enable then` for a std_logic.
- Type conversion `type_mark(expr)` is allowed only between closely related
  types (7.3.5): numeric to numeric, and array to array of the same
  dimension with the same element type. So these are legal:
      unsigned(slv)   signed(slv)   std_logic_vector(uns)   integer(real_x)
  and these are ILLEGAL:
      unsigned(5)  std_logic_vector(7)      -- integer: use to_unsigned
      unsigned(bit_signal)                  -- 1-bit std_logic is not a vector
      std_logic(x)  boolean(x)  integer(slv)
  Use `to_unsigned(n, w)`, `to_signed(n, w)`, `to_integer(u)`,
  `resize(u, w)`. The operand of a conversion may NOT be a string literal,
  a bit-string literal, an aggregate or `null` (7.3.5), so `unsigned("0101")`
  is strictly illegal. Write instead:
      u <= "0101";                     -- literal assigned straight to unsigned
      u <= unsigned'("0101");          -- or a qualified expression
      u <= to_unsigned(5, 4);
- Qualified expression `type_mark'(expr)` states the type when the context
  cannot (7.3.4). Use it for ambiguous overloaded calls
  (`f(std_logic_vector'("0101"))`). Note the apostrophe and parentheses.
- A 1-bit std_logic is NOT a vector: `slv(0)` is std_logic, `slv(0 downto 0)`
  is a 1-bit vector. `x <= sig;` where x is a vector and sig a bit is a type
  mismatch; assign `x(0) <= sig;`. Make a one-bit vector with an aggregate
  `(0 => sig)` (single-element aggregates must be NAMED, 6).

===============================================================
6. AGGREGATES AND `others` (LRM 7.3.2)
===============================================================

- Aggregate = `( [choices =>] expr { , ... } )`. Positional elements come
  first, named ones after, `others =>` last and alone. Every element is
  given exactly once. A single-element aggregate must be named:
  `(0 => '1')`, never `('1')` (that is just a bracketed expression).
- `(others => '0')` needs a place where the size is KNOWN from the context.
  Legal places (7.3.2.2):
    a) an actual for a formal parameter of CONSTRAINED type
    b) the default value of a port of constrained type
    c) the `return` value of a function whose result type is constrained
    d) the right side of an assignment to a declared signal / variable
       (`x <= (others => '0');`)
    e) the initial value of a constant or variable of constrained type
    f) the initial value in a signal declaration of constrained type
    h) the operand of a qualified expression whose type mark is a
       constrained subtype (a type mark is a NAME, so declare one:
       `subtype byte_t is std_logic_vector(7 downto 0);` then
       `byte_t'(others => '0')`)
    i) a part of a larger aggregate that is itself in one of these places
  ILLEGAL (error "OTHERS is illegal aggregate choice for unconstrained
  target"): as an operand of `=`, `/=`, `<`, `+`, `&`, `and`; as an
  argument of a function whose parameter is unconstrained
  (`to_integer((others => '0'))`, `resize((others=>'0'), 8)`); as a `case`
  choice. Fix: compare with a number (`unsigned(x) = 0`), a sized literal
  (`x = "00000000"`) or a named constant of the right type.
- Array aggregates: choices are static values or ranges
  `(0 => a, 1 to 3 => b, others => c)`; a choice that is not static is
  allowed only when it is the single element of the aggregate.
- Record aggregates: by name `(valid => '1', data => x"00")`; `others` only
  when all remaining fields have the same type (7.3.2.1). Prefer separate
  signals over records in pipelines.
- An aggregate as an assignment TARGET splits a value:
  `(carry, sum) <= ...` where every name is a signal/variable and each is
  assigned once (8.4). `others` / ranges are not allowed there.

===============================================================
7. INDEXES, SLICES AND ATTRIBUTES (LRM 6.4, 6.5, 14.1)
===============================================================

- `x(i)` is one element; the index must be inside the range, else the
  simulation stops. A slice's direction must be the SAME as the vector's
  declared direction (6.5):
      signal x : std_logic_vector(7 downto 0);
      x(3 downto 0)     -- correct
      x(0 to 3)         -- ERROR: wrong direction
      x(0 downto 3)     -- legal but EMPTY (null slice, no bits): the next
                        -- assignment then fails with a size mismatch
- The slice bounds must be inside the vector. `x(8 downto 1)` on an
  8-bit vector stops the simulation.
- A slice has its own range: after `y <= x(7 downto 4)`, y needs width 4
  (`y : std_logic_vector(3 downto 0)`). The two sides' bit positions line up
  by position, not by number.
- Attributes you may rely on: `x'length`, `x'high`, `x'low`, `x'left`,
  `x'right`, `x'range`, `x'reverse_range`, `x'ascending`, `x'event`,
  `t'image(v)` (testbenches only). `for i in x'range loop` follows the
  declared direction. `clk'event and clk = '1'` works but use
  `rising_edge(clk)`.
- Attribute `'length` of a port or signal is a constant at build time and is
  the safe way to size a variable: `variable v : unsigned(x'length-1 downto 0);`.

===============================================================
8. SEQUENTIAL STATEMENTS: process, subprogram (LRM 8)
===============================================================

- SIGNAL vs VARIABLE timing (8.4, 8.5, 12.6.4):
    variable  `v := e;`  takes effect IMMEDIATELY, the next line sees it.
    signal    `s <= e;`  takes effect when the process ends (next delta);
                         a later line in the same run still reads the OLD s.
  So never write `s <= a; b <= s + 1;` expecting the new s. Use a
  variable, or compute from `a` directly.
  If a process assigns the same signal several times in one run, the LAST
  assignment wins (8.4.1). This is the standard way to give every signal a
  default at the top of a combinational process and override it in branches.
- A signal assigned in a process that runs only on some branches keeps its
  old value on the others. In a combinational process that is a LATCH.
  Give every signal assigned in a combinational process a value at the top
  of the process, or in every branch, `else` and `when others`.
- A process with a sensitivity list may not contain `wait` (9.2); a process
  without one must contain a `wait` (or it loops forever at time 0).
  Combinational process: list EVERY signal it reads, else simulation and the
  built chip disagree. Clocked process: only `clk` (and an asynchronous
  `rst`). A sensitivity list is plain signal names (no expressions, no
  variables, no constants). [93] no `process(all)`.
- `case expr is when ... end case;` (8.8):
    * every possible value is covered exactly once; the last choice is
      `when others => null;` (a std_logic selector has 9 values, so it ALWAYS
      needs `others`; so does an integer selector);
    * choices are written `when "00" =>`, `when "01" | "10" =>`,
      `when 0 to 3 =>`, `when S_RUN =>`. They must be constants known at
      build time (literals or constants), never signals or function calls;
    * for a vector selector every choice has the SAME LENGTH as the selector,
      and [93] the selector must be a signal/variable/slice/element name, not
      an expression: `case a & b is` and `case std_logic_vector(cnt) is` are
      illegal. Put the value in a named signal first, or `case to_integer(u)
      is` (integer, then every value 0..max or `others`);
    * `others` is last and alone; no `elsif`/`else` in a case.
- `if c then ... elsif c2 then ... else ... end if;` Spelling: `elsif`
  (not `else if`, `elif`), always `end if;`, `then` after each condition.
- Loops: `for i in 0 to 7 loop ... end loop;` The loop variable is an
  implicit constant (never assigned, not declared). The range direction
  must match the loop intent: `for i in 7 downto 0` runs 8 times,
  `for i in 7 to 0` runs ZERO times. Ranges must be fixed (constants or
  generics) for synthesis. `exit` / `next [when cond]` are legal inside.
- `return` (8.12): a FUNCTION must end every path with `return expr;`; a
  PROCEDURE returns nothing. Reaching the end of a function without
  `return` is a run-time error. A function with `if/elsif/else` returning in
  each branch is fine, but add a final `return` anyway.
- Do not use `wait`, `after`, `report`, `assert`, files, `real` and `time` in
  design (src/) files; they are for test programs.

Subprograms (LRM 2):
- Function parameters are `in` only; class is constant (default), signal or
  file. Procedure parameters may be `in`, `out`, `inout`; `out`/`inout`
  default to class `variable`.
- A PURE function (the default) may not read or write any signal or
  variable declared OUTSIDE itself (2.2). A function in the architecture
  that reads an architecture signal is an error. Pass everything it needs
  as parameters, or declare it `impure function`. Prefer passing parameters.
- A function cannot assign signals and cannot `wait`. A procedure may assign
  a signal only if the signal is passed as a `signal ... : out` parameter
  (or the procedure is declared inside the process that owns the driver).
- Declare a subprogram BEFORE use: in the architecture declarative part,
  before `begin` and before the signals that use its types (2.2). A call
  that is textually before its declaration is "not declared".
- Overloading: the same function name may be declared several times for
  different parameter types. A call with a bare literal can then be
  ambiguous (`f("0101")`: slv or unsigned?). Qualify it (5).
- An unconstrained parameter takes the size of the actual: use
  `x'length`, `x'range`, `x'left` inside, never a fixed number.

===============================================================
9. CONCURRENT STATEMENTS (LRM 9)
===============================================================

Everything between the architecture's `begin` and `end` runs in parallel;
the textual order does not matter.
- Conditional assignment (9.5.1), LEGAL in [93] at the architecture level:
      y <= a when sel = '1' else b;
      y <= a when s = "00" else b when s = "01" else c;   -- last `else` needed
  The same form INSIDE a process is [2008] only (see 8: use if/else).
- Selected assignment (9.5.2), LEGAL in [93]:
      with sel select y <= a when "00", b when "01", c when others;
  Same completeness rules as `case` (all values covered, `others` last).
- A concurrent assignment is a process of its own: it is the ONLY writer
  of its target. Do not also assign that target in a clocked process.
- Generate (9.7): `label : for i in 0 to N-1 generate ... end generate label;`
  or `label : if COND generate ... end generate label;`. The label is
  REQUIRED; the range and condition must be fixed at build time (constants,
  generics, `'length`); the loop parameter `i` is a constant. Inside only
  concurrent statements (processes, assignments, instances, nested generate).
  [93] has no `else` / `elsif` / `case` generate: use two `if` generates with
  opposite conditions.
- Component instance (9.6): declare `component NAME port (...); end component;`
  in the architecture declarative part (ports match the entity: names, modes,
  types), then `u_x : NAME port map (...)`. OR instantiate the entity
  directly, which needs no component declaration:
      u_x : entity work.NAME port map (clk => clk, rst => rst, d => sig_d);
- Port map (4.3.2.2, 5.2.1.2): positional items first, then named; prefer
  ALL named (`formal => actual`). Every `in` port without a default must
  be connected; leave an unused OUTPUT as `open`. The actual of an `in`
  formal may be a signal, a constant/literal, a type conversion of a signal
  (`d => std_logic_vector(cnt)`) or `open` only if the formal has a default.
  It may NOT be a computed expression of signals (`d => a & b`,
  `d => not x`): put it in a signal first.
  Direction rule (1.1.1.2): an `in` formal accepts an actual that is an
  `in`/`inout`/`buffer` port or a signal; it does NOT accept an `out`
  port of the enclosing entity. Use the `_i` signal instead.
  Generic map: `generic map (N => 8)`; the formal generic name is on the
  left. Every width mismatch between formal and actual is an error.
- Several instances: give each its own label, unique in the architecture.

===============================================================
10. SIGNALS, DRIVERS AND INITIAL VALUES (LRM 4.3.1.2, 12.6)
===============================================================

- A signal assigned in a process or in a concurrent line has a DRIVER from
  that place. Two places driving the same signal = two drivers:
    * `std_logic` (resolved): it compiles, but the value is 'X' wherever
      the two writers disagree, so the outputs are wrong;
    * `bit`, `integer`, `boolean`, enumerations (unresolved): elaboration
      error "multiple sources".
  One writer per signal (the edition rulebook repeats this).
- Driver size follows the assigned NAME: `x(3) <= v;` drives only bit 3,
  `x(i) <= v;` with a variable index drives the WHOLE vector x. So two
  processes that write different elements of one array/vector with variable
  indexes collide. One process per array.
- A signal with no initial value starts as the left-most value of its type:
  'U' for std_logic. Registers without reset or initial value show 'U'
  until written, and 'U' / 'X' spreads through arithmetic and comparisons
  (`to_integer` on such a vector warns and gives 0). Give every register an
  initial value `:= (others => '0')` in its declaration (allowed: rule d/f of
  6) and/or reset it.
- A signal's value is only updated at the end of the process run; the process
  sees the same value everywhere during its run (8).
- `x <= y after 5 ns;` is ignored by synthesis; do not use `after` in RTL.

===============================================================
11. RUN-TIME STOPS: what each message means and the fix
===============================================================

| message (xvhdl = compile, xelab/xsim = build or run)           | cause                                              | fix |
|----------------------------------------------------------------|----------------------------------------------------|-----|
| syntax error near X                                            | a declaration after `begin`, `;` missing / extra,  | check the line BEFORE the reported one; section 3 |
|                                                                | `else if`, mixed and/or without brackets, sign     | and 5 |
|                                                                | after `*`, reserved word used as a name            |     |
| 'x' is not declared                                            | typo, used before declared, declared in another    | declare in the right place (3), spell the same |
|                                                                | region, missing `use` clause, function after begin |     |
| 'x' is not a variable / illegal in an expression               | `variable` keyword missing in a process, or `:=`   | section 3, 8 |
|                                                                | used on a signal                                   |     |
| cannot read from 'out' object                                  | out port read in the architecture [93]             | `_i` signal |
| type mismatch / no matching overload for "+"                   | operands of different types, integer where a       | convert (5): to_unsigned, unsigned(...), resize |
|                                                                | vector is needed, std_logic vs vector              |     |
| found '0' definitions of operator "<=" (or "+", "=", ...)      | the two sides have different types, e.g.           | convert ONE side so both are signed (or both unsigned): |
|                                                                | `signed(x) <= to_unsigned(2, 12)`; no comparison   | `signed(x) <= to_signed(2, 13)`, or `unsigned(x) <= to_unsigned(2, 12)` |
|                                                                | exists between signed and unsigned                 |     |
| syntax error near 'function' (then 'elsif', 'return', "expected| the function was written AFTER the           | move the whole function up, between `architecture ... is` |
| type void" follow-ons)                                         | architecture's `begin` or inside a process         | and `begin`; the later errors then vanish |
| OTHERS is illegal aggregate choice                             | `(others => '0')` as an operand or argument         | section 6 |
| ambiguous / more than one possible type                        | literal or `&` with no declared target type         | qualify (5) or assign to a signal |
| expression is not BOOLEAN                                      | `if x then` with a std_logic [93]                   | `if x = '1' then` |
| Array sizes do not match, left has N, right has M              | widths differ on `<=`, `:=`, `&` join, product      | count bits; use resize; product = sum of widths |
| index (N) out of range / slice out of range                    | index or slice bound outside the vector             | check bounds, loop ranges |
| value N is outside the range of subtype natural/...            | counter below 0 or above its integer range          | use unsigned, or guard |
| multiple sources / multiple drivers                            | signal written in two places                         | section 10 |
| 'x' has no value / unconnected port with no default             | `in` port left out of a port map                    | connect it |
| function ... did not return a value                            | a path reaches `end function` without `return`      | add `return` on every path |

===============================================================
12. CHECK BEFORE ANSWERING
===============================================================

[ ] no reserved word used as a name; no `_x`, `x_`, `x__y`
[ ] every declaration is in its legal place (3); labels do not clash with signals
[ ] every signal/variable has a fixed range; all vectors use `N-1 downto 0`
[ ] every `if`/`elsif`/`when` condition is a boolean comparison [93]
[ ] no sign after `*`, `**`, `-`, `+`; no mixed and/or without brackets;
    `not` applied to a comparison is bracketed
[ ] `(others => ...)` appears only where the size is known from the target (6)
[ ] every conversion is between closely related types; no conversion of a literal
[ ] no `x(0 to 3)` on a `downto` vector; no empty slice
[ ] every `case` covers everything with `when others`; vector choices have the right length
[ ] every function declared before use, pure functions use only parameters
[ ] every signal written from exactly one place
[ ] every register has an initial value or reset; combinational processes have defaults
[ ] port maps are named, complete and free of computed expressions
