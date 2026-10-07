









class AC_Loiter
{


































    Vector2f get_pilot_desired_acceleration_NE_cmss() const { return get_pilot_desired_acceleration_NE_mss() * 100.0; }



    const Vector2f& get_pilot_desired_acceleration_NE_mss() const { return _desired_accel_ne_mss; }


    void clear_pilot_desired_acceleration() { set_pilot_desired_acceleration_rad(0.0, 0.0); }













    float get_distance_to_target_cm() const { return get_distance_to_target_m() * 100.0; }



    float get_distance_to_target_m() const { return _pos_control.get_pos_error_NE_m(); }



    float get_bearing_to_target_rad() const { return _pos_control.get_bearing_to_target_rad(); }























    float get_roll_cd() const { return _pos_control.get_roll_cd(); }


    float get_pitch_cd() const { return _pos_control.get_pitch_cd(); }


    float get_roll_rad() const { return _pos_control.get_roll_rad(); }


    float get_pitch_rad() const { return _pos_control.get_pitch_rad(); }



    Vector3f get_thrust_vector() const { return _pos_control.get_thrust_vector(); }

    static const struct AP_Param::GroupInfo var_info[];




























    enum class LoiterOption {

    };









};
