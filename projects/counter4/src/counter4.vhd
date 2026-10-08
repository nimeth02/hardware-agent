-- projects/counter4/src/counter4.vhd -- example design: 4-bit up counter
-- with synchronous active-high reset and enable (VHDL-93).

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity counter4 is
  port (
    clk   : in  std_logic;
    rst   : in  std_logic;
    en    : in  std_logic;
    count : out std_logic_vector(3 downto 0)
  );
end entity counter4;

architecture rtl of counter4 is
  signal count_reg : unsigned(3 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        count_reg <= (others => '0');
      elsif en = '1' then
        count_reg <= count_reg + 1;   -- wraps from 15 to 0
      end if;
    end if;
  end process;

  count <= std_logic_vector(count_reg);
end architecture rtl;
