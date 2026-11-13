# tests/test_rtl_fixed_point.py — the fraction-bit check of the RTL linter.

from agents.rtl_agent.lint.rtl_fixed_point import fixed_point_issues, fraction_constants

# The Farrow part and the loop filter of the timing_recovery RTL reviewed on
# 2026-11-10, cut down: one `farrow` function, the same sums again in stage
# registers, and the rulebook's p_loop.
DESIGN = """
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity timing_recovery is
  port (
    aclk          : in  std_logic;
    s_axis_tdata  : in  std_logic_vector(23 downto 0);
    m_axis_tdata  : out std_logic_vector(11 downto 0)
  );
end entity timing_recovery;

architecture rtl of timing_recovery is
  constant I_W   : integer := 12;
  constant ETA_F : integer := 30;
  constant ETA_W : integer := ETA_F + 2;
  constant MU_F  : integer := 16;
  constant MU_W  : integer := MU_F + 2;
  constant MU_ONE : integer := 2**MU_F;
  constant QUARTER : signed(ETA_W-1 downto 0) := shift_left(to_signed(1, ETA_W), ETA_F - 2);
  constant P1_W  : integer := 14 + MU_W;
  constant P2_W  : integer := P1_W + MU_W;
  constant SUM_W : integer := P2_W + 1;
  signal i_s1 : signed(I_W-1 downto 0);
  signal x0_i_s2, x1_i_s2, x2_i_s2, x3_i_s2 : signed(I_W-1 downto 0);
  signal mu_s2, mu_s3, mu_s4, mu_s5 : signed(MU_W-1 downto 0);
  signal v2i_s3 : signed(13 downto 0);
  signal v1i_s3, v1i_s4 : signed(14 downto 0);
  signal v0i_s3, v0i_s4, v0i_s5 : signed(I_W-1 downto 0);
  signal p1i_s4 : signed(P1_W-1 downto 0);
  signal p2i_s5 : signed(P2_W+MU_W-1 downto 0);
  signal ysym_i_s6, ymid_i_s6, yprev_i_r : signed(11 downto 0);
  signal d_i_s8 : signed(12 downto 0);
  signal e_s10 : signed(25 downto 0);
  signal eta_r, acc_r, v_r : signed(ETA_W-1 downto 0);

  function sat12(x : signed) return signed is
  begin
    return resize(x, 12);
  end function;

  function farrow(xm1, xm, xp1, xp2 : signed(I_W-1 downto 0);
                  mu : signed(MU_W-1 downto 0)) return signed is
    variable v2 : signed(13 downto 0);
    variable v1 : signed(14 downto 0);
    variable p1 : signed(P1_W-1 downto 0);
    variable p2 : signed(P2_W+MU_W-1 downto 0);
    variable sum_v : signed(SUM_W-1 downto 0);
  begin
    v2 := resize(xp2, 14) - resize(xp1, 14) - resize(xm, 14) + resize(xm1, 14);
    v2 := shift_right(v2, 1);
    v1 := resize(xp2, 15) - resize(xp1, 15) - resize(xm, 15) - resize(xm1, 15);
    v1 := shift_right(v1, 1);
    p1 := v2 * mu;
    p2 := (resize(p1, P2_W) + resize(v1, P2_W)) * mu;
    sum_v := resize(p2, SUM_W) + resize(xm, SUM_W);
    return sat12(shift_right(sum_v, 15));
  end function;
begin
  p_s : process(aclk)
    variable v2i_v : signed(13 downto 0);
    variable sum_i : signed(SUM_W-1 downto 0);
  begin
    if rising_edge(aclk) then
      i_s1 <= signed(s_axis_tdata(23 downto 12));
      x0_i_s2 <= i_s1;
      x1_i_s2 <= x0_i_s2;
      x2_i_s2 <= x1_i_s2;
      x3_i_s2 <= x2_i_s2;
      v2i_v := resize(x3_i_s2, 14) - resize(x2_i_s2, 14) - resize(x1_i_s2, 14) + resize(x0_i_s2, 14);
      v2i_v := shift_right(v2i_v, 1);
      v2i_s3 <= v2i_v;
      v1i_s3 <= shift_right(resize(x3_i_s2, 15) - resize(x2_i_s2, 15) - resize(x1_i_s2, 15) - resize(x0_i_s2, 15), 1);
      v0i_s3 <= x1_i_s2;
      mu_s3 <= mu_s2;
      p1i_s4 <= v2i_s3 * mu_s3;
      v1i_s4 <= v1i_s3;
      v0i_s4 <= v0i_s3;
      mu_s4 <= mu_s3;
      p2i_s5 <= (resize(p1i_s4, P2_W) + resize(v1i_s4, P2_W)) * mu_s4;
      v0i_s5 <= v0i_s4;
      mu_s5 <= mu_s4;
      sum_i := resize(p2i_s5, SUM_W) + resize(v0i_s5, SUM_W);
      ysym_i_s6 <= sat12(shift_right(sum_i, 15));
      ymid_i_s6 <= farrow(x0_i_s2, x1_i_s2, x2_i_s2, x3_i_s2, mu_s5);
      d_i_s8 <= resize(ysym_i_s6, 13) - resize(yprev_i_r, 13);
      e_s10 <= resize(ymid_i_s6 * d_i_s8, 26);
    end if;
  end process p_s;

  p_loop : process(aclk)
    variable eta_next : signed(ETA_W-1 downto 0);
    variable mu_v : signed(ETA_W-1 downto 0);
    variable e_w : signed(ETA_W-1 downto 0);
    variable acc_next : signed(ETA_W-1 downto 0);
  begin
    if rising_edge(aclk) then
      eta_next := eta_r - (QUARTER + v_r);
      mu_v := shift_right(eta_r, ETA_F - MU_F - 2);
      if mu_v >= MU_ONE then
        mu_s2 <= to_signed(MU_ONE - 1, MU_W);
      else
        mu_s2 <= resize(mu_v, MU_W);
      end if;
      eta_r <= eta_next;
      yprev_i_r <= ysym_i_s6;
      e_w := resize(e_s10, ETA_W);
      acc_next := acc_r + shift_right(e_w, 7);
      acc_r <= acc_next;
      v_r <= shift_left(e_w, 1) + acc_next;
    end if;
  end process p_loop;

  m_axis_tdata <= std_logic_vector(ysym_i_s6);
end architecture rtl;
"""

# every product shifted back by MU_F before the next add
FIXES = [
    ("p1 := v2 * mu;", "p1 := shift_right(v2 * mu, MU_F);"),
    ("p2 := (resize(p1, P2_W) + resize(v1, P2_W)) * mu;",
     "p2 := shift_right((resize(p1, P2_W) + resize(v1, P2_W)) * mu, MU_F);"),
    ("p1i_s4 <= v2i_s3 * mu_s3;", "p1i_s4 <= shift_right(v2i_s3 * mu_s3, MU_F);"),
    ("p2i_s5 <= (resize(p1i_s4, P2_W) + resize(v1i_s4, P2_W)) * mu_s4;",
     "p2i_s5 <= shift_right((resize(p1i_s4, P2_W) + resize(v1i_s4, P2_W)) * mu_s4, MU_F);"),
]


def _fixed(design: str = DESIGN) -> str:
    for old, new in FIXES:
        assert old in design
        design = design.replace(old, new)
    return design


def _line(text: str, design: str = DESIGN) -> int:
    return next(n for n, line in enumerate(design.splitlines(), 1) if text in line)


def test_fraction_constants_are_read_from_the_f_names():
    assert fraction_constants({"mu_f": 16, "eta_f": 30, "mu_w": 18, "i_w": 12}) == {"mu": 16, "eta": 30}


def test_products_added_before_being_shifted_back_are_reported_through_variables_registers_and_functions():
    """Reviewed timing_recovery RTL of 2026-11-10: v2*mu (16 fraction bits from
    mu) was added to the plain v1, the sum times mu was added to the plain
    sample, and one shift by 15 at the end was meant to repair both."""
    found = fixed_point_issues(DESIGN)
    assert {i["code"] for i in found} == {"fraction_bits_misaligned"}
    lines = {int(i["detail"].split(":")[0].removeprefix("line ")) for i in found}
    assert lines == {
        _line("p2 := (resize(p1, P2_W)"),            # inside the function, mu's scale taken from the call
        _line("sum_v := resize(p2, SUM_W)"),
        _line("p2i_s5 <= (resize(p1i_s4, P2_W)"),    # the same sums spread over stage registers
        _line("sum_i := resize(p2i_s5, SUM_W)"),
    }
    first = next(i["detail"] for i in found if "p2i_s5 <=" in i["detail"].split(">>")[1])
    assert "`resize(p1i_s4, P2_W)` has 16 more fraction bits than `resize(v1i_s4, P2_W)`" in first
    assert "ETA_F = 30, MU_F = 16" in first
    # the fix names the constant, and does not count the 0.5 gain (shift by 1) as part of it
    assert "`shift_right(resize(p1i_s4, P2_W), MU_F)`" in first


def test_a_design_that_shifts_every_product_back_is_clean():
    assert fixed_point_issues(_fixed()) == []


def test_gains_written_as_shifts_are_not_reported():
    """`shift_right(v2, 1)` is 0.5 * v2 and `K1*e + acc` adds two shifts of the
    same error (2**-7 and 2**-15 apart): neither is a misplaced binary point,
    also when the file's smallest fraction is only 8 bits."""
    small = _fixed().replace("constant MU_F  : integer := 16;", "constant MU_F  : integer := 8;")
    assert fixed_point_issues(small) == []
    loop = _fixed().replace("acc_next := acc_r + shift_right(e_w, 7);", "acc_next := acc_r + shift_right(e_w, 15);")
    assert fixed_point_issues(loop) == []


def test_a_file_without_fraction_constants_is_not_checked():
    plain = DESIGN.replace("MU_F", "MU_FRACTION_COUNT").replace("ETA_F", "ETA_FRACTION_COUNT")
    assert fixed_point_issues(plain) == []


def test_a_value_built_from_a_fraction_keeps_its_own_scale():
    """`mu_sq_s4 <= mu_s3 * mu_s3` has 32 fraction bits, not MU_F: the name
    rule covers mu, mu_s3, mu_v only, so shifting by 2*MU_F is accepted."""
    squared = _fixed().replace(
        "      mu_s5 <= mu_s4;",
        "      mu_s5 <= mu_s4;\n      mu_sq_s4 <= mu_s3 * mu_s3;\n"
        "      w_s5 <= shift_right(v0i_s4 * mu_sq_s4, 2*MU_F) + v0i_s4;")
    assert fixed_point_issues(squared) == []
    wrong = squared.replace("shift_right(v0i_s4 * mu_sq_s4, 2*MU_F)", "shift_right(v0i_s4 * mu_sq_s4, MU_F)")
    found = fixed_point_issues(wrong)
    assert len(found) == 1 and "MU_F" in found[0]["detail"] and "w_s5 <=" in found[0]["detail"]


def test_lint_rtl_reports_each_add_once(monkeypatch):
    """product_added_unscaled and the fraction check both see a product added
    to a plain signal; the line is explained once. A product that went
    through one more register first (`p2d_s6 <= p2i_s5`) is only seen by the
    fraction check."""
    from agents.rtl_agent.lint import rtl_linter

    monkeypatch.setattr(rtl_linter, "_compile", lambda *a, **k: (None, []))
    both = ("fraction_bits_misaligned", "product_added_unscaled")
    issues = rtl_linter.lint_rtl({"entity_name": "timing_recovery"}, {"code": DESIGN})["issues"]
    lines = [i["detail"].split(":")[0] for i in issues if i["code"] in both]
    assert len(lines) == 4 and len(set(lines)) == 4
    delayed = DESIGN.replace("sum_i := resize(p2i_s5, SUM_W) + resize(v0i_s5, SUM_W);",
                             "p2d_s6 <= p2i_s5;\n      sum_i := resize(p2d_s6, SUM_W) + resize(v0i_s5, SUM_W);")
    issues = rtl_linter.lint_rtl({"entity_name": "timing_recovery"}, {"code": delayed})["issues"]
    assert [i["code"] for i in issues if "sum_i := resize(p2d_s6" in i["detail"]] == ["fraction_bits_misaligned"]
    clean = rtl_linter.lint_rtl({"entity_name": "timing_recovery"}, {"code": _fixed()})["issues"]
    assert not [i for i in clean if i["code"] in both]


# -- the spec's weighted sums, worked out again from the RTL --------------------
SPEC_LINES = [
    "Compute y_sym = farrow(x[m-1], x[m], x[m+1], x[m+2], mu) at interpolation instant n-2+mu.",
    "Farrow interpolator uses piecewise-parabolic form with alpha=1/2: v2 = 0.5*(x[m+2]-x[m+1]-x[m]+x[m-1]), "
    "v1 = -0.5*x[m+2]+1.5*x[m+1]-0.5*x[m]-0.5*x[m-1], v0 = x[m], y = (v2*mu + v1)*mu + v0.",
    "Update PI loop: acc = acc + K2 * e; v = K1 * e + acc; y_prev = y_sym.",
]


def test_weighted_sums_are_read_from_the_spec_text():
    from fractions import Fraction as F
    from agents.rtl_agent.lint.rtl_equation_check import spec_formulas

    found = spec_formulas(SPEC_LINES)
    assert set(found) == {"v2", "v1"}                   # v0 = x[m] is a copy; y, acc, v are not sums of samples
    assert found["v1"][1] == {"x[m+2]": F(-1, 2), "x[m+1]": F(3, 2), "x[m]": F(-1, 2), "x[m-1]": F(-1, 2)}
    assert found["v2"][1] == {"x[m+2]": F(1, 2), "x[m+1]": F(-1, 2), "x[m]": F(-1, 2), "x[m-1]": F(1, 2)}


def test_a_wrong_weight_in_a_spec_formula_is_reported_once_per_copy():
    """Reviewed timing_recovery RTL of 2026-11-10: v1 was 0.5*(x3 - x2 - x1 - x0),
    the spec's weights are -0.5, 1.5, -0.5, -0.5. v2 was right."""
    from agents.rtl_agent.lint.rtl_equation_check import equation_issues

    found = equation_issues(SPEC_LINES, DESIGN)
    assert {i["code"] for i in found} == {"equation_coefficients_differ"}
    # the function's v1 and the stage register v1i_s3; the second write `v1 := shift_right(v1, 1)` is the same sum
    assert len(found) == 2 and all("`v1" in i["detail"] for i in found)
    detail = found[0]["detail"]
    assert "its weights are -1, -1, -1, 1" in detail and "its weights are -0.5, -0.5, -0.5, 1.5" in detail
    assert "3*x is `shift_left(x, 1) + x`" in detail and ">> v1 := resize(xp2, 15)" in detail


def test_the_right_weights_pass_in_either_form():
    from agents.rtl_agent.lint.rtl_equation_check import equation_issues

    doubled = DESIGN.replace(                           # twice the value, shifted once: -x + 3x - x - x
        "v1 := resize(xp2, 15) - resize(xp1, 15) - resize(xm, 15) - resize(xm1, 15);",
        "v1 := shift_left(resize(xp1, 15), 1) + resize(xp1, 15) - resize(xp2, 15) - resize(xm, 15) - resize(xm1, 15);"
    ).replace(                                          # term by term: 1.5*x = x + x/2
        "v1i_s3 <= shift_right(resize(x3_i_s2, 15) - resize(x2_i_s2, 15) - resize(x1_i_s2, 15) - resize(x0_i_s2, 15), 1);",
        "v1i_s3 <= resize(x2_i_s2, 15) + shift_right(resize(x2_i_s2, 15), 1) - shift_right(resize(x3_i_s2, 15), 1)"
        " - shift_right(resize(x1_i_s2, 15), 1) - shift_right(resize(x0_i_s2, 15), 1);")
    assert doubled != DESIGN and equation_issues(SPEC_LINES, doubled) == []
    # the usual mistake: shift_left(x, 1) for the 1.5 term is 2*x, 1.0 after the final shift
    usual = doubled.replace("shift_left(resize(xp1, 15), 1) + resize(xp1, 15)", "shift_left(resize(xp1, 15), 1)")
    found = equation_issues(SPEC_LINES, usual)
    assert len(found) == 1 and "its weights are -1, -1, -1, 2" in found[0]["detail"]


def test_lines_that_are_not_weighted_sums_and_specs_without_formulas_are_left_alone():
    from agents.rtl_agent.lint.rtl_equation_check import equation_issues

    assert equation_issues(["Output is the sum of a and b."], DESIGN) == []
    # v1 delayed one row is a copy; a v1 built with a multiply by a signal is not read
    other = DESIGN.replace("v1 := shift_right(v1, 1);", "v1 := shift_right(v1, 1);\n    v1 := resize(v1 * mu, 15);")
    assert len(equation_issues(SPEC_LINES, other)) == 2


# -- the spec's product formula, multiplied out again from the RTL --------------
TERM_BY_TERM_V1 = (
    "v1i_s3 <= shift_right(resize(x3_i_s2, 15) - resize(x2_i_s2, 15) - resize(x1_i_s2, 15) - resize(x0_i_s2, 15), 1);",
    "v1i_s3 <= resize(x2_i_s2, 15) + shift_right(resize(x2_i_s2, 15), 1) - shift_right(resize(x3_i_s2, 15), 1)"
    " - shift_right(resize(x1_i_s2, 15), 1) - shift_right(resize(x0_i_s2, 15), 1);")


def test_product_formulas_are_read_from_the_spec_text():
    from agents.rtl_agent.lint.rtl_equation_check import spec_products

    found = spec_products(SPEC_LINES)
    assert set(found["y"][1]) == {("mu", "mu", "v2"), ("mu", "v1"), ("v0",)}
    assert "v2" not in found and "v1" not in found      # sums of samples: the weighted-sum check


def test_a_formula_multiplied_only_once_is_reported():
    """timing_recovery 2026-11-11 13:09: p1 = v2*mu, p2 = v1*mu, sum = p1 + p2 + v0
    is (v2 + v1)*mu + v0; the spec's y = (v2*mu + v1)*mu + v0 needs mu twice."""
    from agents.rtl_agent.lint.rtl_equation_check import polynomial_issues

    assert polynomial_issues(SPEC_LINES, DESIGN) == []
    assert polynomial_issues(SPEC_LINES, _fixed()) == []
    once = DESIGN.replace("p2i_s5 <= (resize(p1i_s4, P2_W) + resize(v1i_s4, P2_W)) * mu_s4;",
                          "p2i_s5 <= resize(p1i_s4, P2_W) + resize(v1i_s4 * mu_s4, P2_W);")
    found = polynomial_issues(SPEC_LINES, once)
    assert [i["code"] for i in found] == ["equation_terms_differ"]
    detail = found[0]["detail"]
    assert detail.startswith(f"line {_line('sum_i := resize(p2i_s5', once)}: `sum_i`")
    assert "sum of these products: v2*mu, v1*mu, v0" in detail and "Missing: v2*mu*mu." in detail
    assert polynomial_issues(["Output is the sum of a and b."], once) == []


def test_a_value_kept_at_twice_its_formula_and_never_halved_is_reported():
    """v2 without its 0.5 passes the weighted-sum check (twice the value is
    allowed when it is shifted back later); here nothing shifts it back."""
    from agents.rtl_agent.lint.rtl_equation_check import equation_issues, polynomial_issues

    right = _fixed().replace(*TERM_BY_TERM_V1)
    assert right != _fixed() and polynomial_issues(SPEC_LINES, right) == []
    doubled = right.replace("      v2i_v := shift_right(v2i_v, 1);\n", "")
    assert doubled != right
    assert not [i for i in equation_issues(SPEC_LINES, doubled) if "`v2i" in i["detail"]]
    found = polynomial_issues(SPEC_LINES, doubled)
    assert [i["code"] for i in found] == ["equation_value_not_scaled_back"]
    assert "`v2i_s3` holds 2 times the spec's `v2`" in found[0]["detail"]
    assert "`shift_right(..., 1)`" in found[0]["detail"]
    # twice the value with one more bit shifted off its product: the same number
    repaid = doubled.replace("p1i_s4 <= shift_right(v2i_s3 * mu_s3, MU_F);",
                             "p1i_s4 <= shift_right(v2i_s3 * mu_s3, MU_F + 1);")
    assert repaid != doubled and polynomial_issues(SPEC_LINES, repaid) == []


# -- the spec's two loop gains, compared as "how many times apart" ---------------
LOOP_SPEC = SPEC_LINES + ["Constants: N=4, K1=2^-7, K2=2^-15."]
K2_LINE = "acc_next := acc_r + shift_right(e_w, 7);"
K1_LINE = "v_r <= shift_left(e_w, 1) + acc_next;"


def test_loop_constants_and_the_gain_pair_are_read_from_the_spec_text():
    from fractions import Fraction as F
    from agents.rtl_agent.lint.rtl_equation_check import spec_loop_gains, spec_numbers

    numbers = spec_numbers(LOOP_SPEC + ["If strobe: m = n - 2.", "alpha = 1/2, step = 0.25."])
    assert numbers["k1"] == F(1, 128) and numbers["k2"] == F(1, 32768) and numbers["n"] == 4
    assert numbers["alpha"] == F(1, 2) and numbers["step"] == F(1, 4) and "m" not in numbers
    (loop,) = spec_loop_gains(LOOP_SPEC)
    assert (loop["total"], loop["result"], loop["error"]) == ("acc", "v", "e")
    assert (loop["gain_result"], loop["gain_total"], loop["wanted"]) == ("k1", "k2", 256)
    assert spec_loop_gains(SPEC_LINES) == []            # K1 and K2 without a value: nothing to compare
    worded = ["On strobe: acc = acc + K2 * e where K2 = 2^-15.", "On strobe: v = K1 * e + acc where K1 = 2^-7.",
              "Update acc = acc + K2 * e, v = K1 * e + acc, and y_prev = y_sym."]
    assert [l["wanted"] for l in spec_loop_gains(worded[:2])] == [256]
    assert [l["wanted"] for l in spec_loop_gains(worded[2:] + ["N=4, K1=2^-7, K2=2^-15."])] == [256]


def test_loop_gains_the_right_number_of_times_apart_pass_on_any_scale():
    """K1 = 2^-7 is 256 times K2 = 2^-15. The rulebook's lines (2*e and e/128)
    are 256 apart; so are e/128 and e/32768, and a result that reads the total
    before this update."""
    from agents.rtl_agent.lint.rtl_equation_check import loop_gain_issues

    assert K2_LINE in DESIGN and K1_LINE in DESIGN
    assert loop_gain_issues(LOOP_SPEC, DESIGN) == []
    other_scale = DESIGN.replace(K2_LINE, "acc_next := acc_r + shift_right(e_w, 15);") \
                        .replace(K1_LINE, "v_r <= shift_right(e_w, 7) + acc_next;")
    assert loop_gain_issues(LOOP_SPEC, other_scale) == []
    old_total = DESIGN.replace(K1_LINE, "v_r <= shift_left(e_w, 1) + acc_r;")
    assert loop_gain_issues(LOOP_SPEC, old_total) == []
    assert loop_gain_issues(SPEC_LINES, DESIGN.replace(K1_LINE, "v_r <= e_w + acc_next;")) == []


def test_loop_gains_the_wrong_number_of_times_apart_are_reported():
    """Saved timing_recovery files: `acc + shift_right(e, 8)` with
    `shift_right(e, 7) + acc` (K1 only 2 times K2), and `acc + e` with
    `e + acc` (the same gain twice)."""
    from agents.rtl_agent.lint.rtl_equation_check import loop_gain_issues

    twice = DESIGN.replace(K2_LINE, "acc_next := acc_r + shift_right(e_w, 8);") \
                  .replace(K1_LINE, "v_r <= shift_right(e_w, 7) + acc_next;")
    found = loop_gain_issues(LOOP_SPEC, twice)
    assert [i["code"] for i in found] == ["loop_gain_ratio"]
    detail = found[0]["detail"]
    assert detail.startswith(f"line {_line('v_r <= shift_right(e_w, 7)', twice)}: ")
    assert "the first is 3 times the second" in detail and "K1 is 2^8 times K2" in detail
    assert "write the K1 term of `v_r` as `e_s10`" in detail          # 256 * e/256
    same = DESIGN.replace(K2_LINE, "acc_next := acc_r + e_w;").replace(K1_LINE, "v_r <= e_w + acc_r;")
    found = loop_gain_issues(LOOP_SPEC, same)
    assert len(found) == 1 and "the first is 1 times the second" in found[0]["detail"]
    assert "`shift_left(e_s10, 8)`" in found[0]["detail"]


def test_lint_rtl_runs_the_loop_gain_check(monkeypatch):
    from agents.rtl_agent.lint import rtl_linter

    monkeypatch.setattr(rtl_linter, "_compile", lambda *a, **k: (None, []))
    spec = {"entity_name": "timing_recovery", "behavior": LOOP_SPEC}
    same = DESIGN.replace(K2_LINE, "acc_next := acc_r + e_w;").replace(K1_LINE, "v_r <= e_w + acc_r;")
    assert "loop_gain_ratio" in [i["code"] for i in rtl_linter.lint_rtl(spec, {"code": same})["issues"]]
    assert "loop_gain_ratio" not in [i["code"] for i in rtl_linter.lint_rtl(spec, {"code": DESIGN})["issues"]]
