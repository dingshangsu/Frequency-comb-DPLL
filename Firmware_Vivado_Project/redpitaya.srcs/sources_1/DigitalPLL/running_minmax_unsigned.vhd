library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;


entity running_minmax_unsigned is
Generic (
    DATA_WIDTH    : integer := 16
);
port (
    clk             : in  std_logic;
    
    clk_enable      : in  std_logic;
    data            : in  std_logic_vector(DATA_WIDTH-1 downto 0);    -- unsigned
    
    -- output port: min_value and max_value update 1 clk cycle after assertion of dump_results
    dump_results    : in  std_logic;
    min_value       : out std_logic_vector(DATA_WIDTH-1 downto 0);
    max_value       : out std_logic_vector(DATA_WIDTH-1 downto 0)

    );
end entity;

architecture Behavioral of running_minmax_unsigned is

    signal current_min        : unsigned(DATA_WIDTH-1 downto 0) := (others => '0');
    signal current_max        : unsigned(DATA_WIDTH-1 downto 0) := (others => '0');
    
    signal min_value_internal : unsigned(DATA_WIDTH-1 downto 0) := (others => '0');
    signal max_value_internal : unsigned(DATA_WIDTH-1 downto 0) := (others => '0');

begin

    process (clk)
    begin
        if rising_edge(clk) then
            if dump_results = '1' then
                -- need to dump our results and reset
                min_value_internal <= current_min;
                max_value_internal <= current_max;
                current_min <= unsigned(data);
                current_max <= unsigned(data);

            else
                -- normal mode:
                if clk_enable = '1' then
                    if unsigned(data) > current_max then
                        current_max <= unsigned(data);
                    end if;
                    if unsigned(data) < current_min then
                        current_min <= unsigned(data);
                    end if;

                end if;
            end if;

        end if;
    end process;


    min_value <= std_logic_vector(min_value_internal);
    max_value <= std_logic_vector(max_value_internal);

end;